# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Whisper JAX pipeline compatible with Distil Whisper checkpoints. Copied from https://github.com/sanchit-gandhi/whisper-jax/blob/main/whisper_jax/pipeline.py"""

import math

import jax
import jax.numpy as jnp
import numpy as np
import requests
import torch
from flax import jax_utils
from flax.core.frozen_dict import freeze
from flax.training.common_utils import shard
from transformers import WhisperFeatureExtractor, WhisperTokenizerFast
from transformers.models.whisper.tokenization_whisper import TO_LANGUAGE_CODE
from transformers.pipelines.audio_utils import ffmpeg_read
from transformers.utils import logging

from .modeling_flax_whisper import FlaxWhisperForConditionalGeneration


logger = logging.get_logger(__name__)

class FlaxWhisperFeatureExtractor(WhisperFeatureExtractor):
    def _np_extract_fbank_features(self, waveform: np.array) -> np.ndarray:
        """
        Compute the log-mel spectrogram of the provided audio using torch filters. Using the torch implementation
        computes stft filter banks approx 5x faster than its numpy counterpart, which is the native implementation
        in transformers, and matches to within 1e-5 abs tolerance.
        """
        waveform = torch.from_numpy(waveform).type(torch.float32)

        window = torch.hann_window(self.n_fft)
        stft = torch.stft(waveform, self.n_fft, self.hop_length, window=window, return_complex=True)
        magnitudes = stft[..., :-1].abs() ** 2

        mel_filters = torch.from_numpy(self.mel_filters).type(torch.float32)
        mel_spec = mel_filters.T @ magnitudes

        log_spec = torch.clamp(mel_spec, min=1e-10).log10()
        log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
        log_spec = (log_spec + 4.0) / 4.0
        return log_spec.numpy()


class FlaxWhisperPipeline:
    def __init__(
        self,
        checkpoint="openai/whisper-large-v2",
        dtype=jnp.float32,
        batch_size=None,
        max_length=None,
        **kwargs,
    ):
        """
        Args
            checkpoint (`str`, *optional*, defaults to `"openai/whisper-large-v2"):
                The Whisper checkpoint to use with the pipeline. Must be an available checkpoint on the Hugging Face Hub
                with Flax weights.
            dtype (`jax.numpy.dtype`, *optional*, defaults to `jax.numpy.float32`):
                The data type of the computation. Can be one of `jax.numpy.float32`, `jax.numpy.float16` (on GPUs) and
                `jax.numpy.bfloat16` (on TPUs). This can be used to enable half-precision inference on GPUs or TPUs.
                If specified all the computation will be performed with the given `dtype`. **Note that this only
                specifies the dtype of the computation and does not influence the dtype of model parameters.**
            batch_size (`int`, *optional*, defaults to the minimum per-device batch size, i.e. `jax.local_device_count()`):
                The batch size to be used in chunking transcription. Beneficial for transcribing long audio files. Passing
                a batch size in the `__init__` method will be superseded by any batch size passed to the `__call__` method.
            max_length (`int`, *optional*):
                The maximum numbers of tokens to generate. Defaults to `model.config.max_length`.
        """
        self.checkpoint = checkpoint
        self.dtype = dtype

        self.feature_extractor = FlaxWhisperFeatureExtractor.from_pretrained(self.checkpoint)
        self.tokenizer = WhisperTokenizerFast.from_pretrained(self.checkpoint)

        self.model, self.params = FlaxWhisperForConditionalGeneration.from_pretrained(
            self.checkpoint,
            _do_init=False,
            dtype=self.dtype,
            **kwargs,
        )

        self.max_length = max_length if max_length is not None else self.model.generation_config.max_length
        self.min_batch_size = jax.local_device_count()
        self.batch_size = (
            batch_size if batch_size is not None else self.min_batch_size
        )  # we need a minimum of 1 batch per-device

        def generate(
            params,
            input_features,
            forced_decoder_ids,
            return_timestamps,
            num_beams,
            length_penalty,
            do_sample,
            top_k,
            temperature,
        ):
            output_ids = self.model.pipeline_generate(
                input_features,
                params=params,
                forced_decoder_ids=forced_decoder_ids,
                return_timestamps=return_timestamps,
                max_length=self.max_length,
                num_beams=num_beams,
                length_penalty=length_penalty,
                do_sample=do_sample,
                top_k=top_k,
                temperature=temperature,
            )
            return output_ids

        self.params = jax_utils.replicate(self.params)
        self.p_generate = jax.pmap(
            generate,
            "input_features",
            in_axes=(0, 0, None, None, None, None, None, None, None),
            static_broadcasted_argnums=(
                3,
                4,
                5,
                6,
                7,
                8,
            ),
        )

    def generate(
        self,
        input_features,
        language=None,
        task=None,
        return_timestamps=False,
        num_beams=5,
        length_penalty=1.0,
        do_sample=False,
        top_k=50,
        temperature=1.0,
    ):
        forced_decoder_ids = self.get_forced_decoder_ids(
            language=language, task=task, return_timestamps=return_timestamps
        )
        # if we're using pmap we need to manually replicate the input data across devices and gather the output tokens
        output_ids = self.p_generate(
            freeze(self.params),
            shard(input_features),
            forced_decoder_ids,
            return_timestamps,
            num_beams,
            length_penalty,
            do_sample,
            top_k,
            temperature,
        ).sequences
        output_ids = jax.device_get(output_ids.reshape(-1, self.max_length))
        logger.info(f"Transcribing with language={language},task={task},num_beams={num_beams},length_penalty={length_penalty},top_k={top_k},temperature={temperature}")
        
        return output_ids

    def get_forced_decoder_ids(self, generation_config=None, task=None, language=None, return_timestamps=False):
        if generation_config is None:
            generation_config = self.model.generation_config

        if hasattr(generation_config, "is_multilingual"):
            is_multilingual = generation_config.is_multilingual
        else:
            is_multilingual = None

        forced_decoder_ids = []

        if is_multilingual:
            if language is not None:
                language = language.lower()
                if language in generation_config.lang_to_id.keys():
                    language_token = language
                elif language in TO_LANGUAGE_CODE.values():
                    language_token = f"<|{language}|>"
                elif language in TO_LANGUAGE_CODE.keys():
                    language_token = f"<|{TO_LANGUAGE_CODE[language]}|>"
                else:
                    if len(language) == 2:
                        # ISO 639-1 language code
                        acceptable_languages = list(TO_LANGUAGE_CODE.values())
                    elif "<" in language or "|" in language or ">" in language:
                        # generation config language code
                        acceptable_languages = list(generation_config.lang_to_id.keys())
                    else:
                        # language passed as a string
                        acceptable_languages = list(TO_LANGUAGE_CODE.keys())
                    raise ValueError(
                        f"Unsupported language: {language}. Language should be one of:" f" {acceptable_languages}."
                    )
                forced_decoder_ids.append((1, generation_config.lang_to_id[language_token]))

            if task is not None:
                forced_decoder_ids.append((2, generation_config.task_to_id[task]))
            else:
                forced_decoder_ids.append((2, generation_config.task_to_id["transcribe"]))

        if not return_timestamps:
            if forced_decoder_ids and forced_decoder_ids[-1][0] != generation_config.no_timestamps_token_id:
                idx = forced_decoder_ids[-1][0] + 1 if forced_decoder_ids else 1
                forced_decoder_ids.append((idx, generation_config.no_timestamps_token_id))
            else:
                forced_decoder_ids.append((1, generation_config.no_timestamps_token_id))

        return forced_decoder_ids

    def chunk_iter_with_batch(self, inputs, chunk_len, stride_left, stride_right, batch_size):
        inputs_len = inputs.shape[0]
        step = chunk_len - stride_left - stride_right

        all_chunk_start_idx = np.arange(0, inputs_len, step)
        num_samples = len(all_chunk_start_idx)

        num_batches = math.ceil(num_samples / batch_size)
        batch_idx = np.array_split(np.arange(num_samples), num_batches)

        for idx in batch_idx:
            chunk_start_idx = all_chunk_start_idx[idx]

            chunk_end_idx = chunk_start_idx + chunk_len

            chunks = [inputs[chunk_start:chunk_end] for chunk_start, chunk_end in zip(chunk_start_idx, chunk_end_idx)]
            processed = self.feature_extractor(
                chunks, sampling_rate=self.feature_extractor.sampling_rate, return_tensors="np"
            )

            _stride_left = np.where(chunk_start_idx == 0, 0, stride_left)
            is_last = np.where(stride_right > 0, chunk_end_idx > inputs_len, chunk_end_idx >= inputs_len)
            _stride_right = np.where(is_last, 0, stride_right)

            chunk_lens = [chunk.shape[0] for chunk in chunks]
            strides = [
                (chunk_l, _stride_l, _stride_r)
                for chunk_l, _stride_l, _stride_r in zip(chunk_lens, _stride_left, _stride_right)
            ]

            yield {"stride": strides, **processed}

    def preprocess_batch(self, inputs, chunk_length_s=30.0, stride_length_s=None, batch_size=None):
        if isinstance(inputs, np.ndarray):
            logger.warning(
                "Numpy array passed as input - no sampling rate checks will be performed."
                "It is strongly recommended to pass the input as a dictionary with an 'array' key "
                "containing the numpy array representing the audio, and a 'sampling_rate' key "
                "containing the sampling rate associated with the audio array."
                "Failing to do so can result in silent errors that might be hard to debug."
            )

        if isinstance(inputs, str):
            if inputs.startswith("http://") or inputs.startswith("https://"):
                # We need to actually check for a real protocol, otherwise it's impossible to use a local file
                # like http_huggingface_co.png
                inputs = requests.get(inputs).content
            else:
                with open(inputs, "rb") as f:
                    inputs = f.read()

        if isinstance(inputs, bytes):
            inputs = ffmpeg_read(inputs, self.feature_extractor.sampling_rate)

        stride = None
        if isinstance(inputs, dict):
            stride = inputs.get("stride", None)
            # Accepting `"array"` which is the key defined in `datasets` for
            # better integration
            if not ("sampling_rate" in inputs and "array" in inputs):
                raise ValueError(
                    "When passing a dictionary to FlaxWhisperPipline, the dict needs to contain an 'array' key "
                    "containing the numpy array representing the audio, and a 'sampling_rate' key "
                    "containing the sampling rate associated with the audio array."
                )

            in_sampling_rate = inputs.get("sampling_rate")
            inputs = inputs.get("array", None)

            if in_sampling_rate != self.feature_extractor.sampling_rate:
                try:
                    import librosa
                except ImportError as err:
                    raise ImportError(
                        "To support resampling audio files, please install 'librosa' and 'soundfile'."
                    ) from err

                inputs = librosa.resample(
                    inputs, orig_sr=in_sampling_rate, target_sr=self.feature_extractor.sampling_rate
                )
                ratio = self.feature_extractor.sampling_rate / in_sampling_rate
            else:
                ratio = 1

        if not isinstance(inputs, np.ndarray):
            raise ValueError(f"We expect a numpy ndarray as input, got `{type(inputs)}`")
        if len(inputs.shape) != 1:
            raise ValueError("We expect a single channel audio input for AutomaticSpeechRecognitionPipeline")

        if stride is not None:
            if stride[0] + stride[1] > inputs.shape[0]:
                raise ValueError("Stride is too large for input")

            # Stride needs to get the chunk length here, it's going to get
            # swallowed by the `feature_extractor` later, and then batching
            # can add extra data in the inputs, so we need to keep track
            # of the original length in the stride so we can cut properly.
            stride = (inputs.shape[0], int(round(stride[0] * ratio)), int(round(stride[1] * ratio)))

        if chunk_length_s:
            if stride_length_s is None:
                stride_length_s = chunk_length_s / 6

            if isinstance(stride_length_s, (int, float)):
                stride_length_s = [stride_length_s, stride_length_s]

            chunk_len = round(chunk_length_s * self.feature_extractor.sampling_rate)
            stride_left = round(stride_length_s[0] * self.feature_extractor.sampling_rate)
            stride_right = round(stride_length_s[1] * self.feature_extractor.sampling_rate)

            if chunk_len < stride_left + stride_right:
                raise ValueError("Chunk length must be superior to stride length")

            for item in self.chunk_iter_with_batch(
                inputs,
                chunk_len,
                stride_left,
                stride_right,
                batch_size,
            ):
                yield item
        else:
            processed = self.feature_extractor(
                inputs, sampling_rate=self.feature_extractor.sampling_rate, return_tensors="np"
            )
            if stride is not None:
                processed["stride"] = stride
            yield processed

    def postprocess(self, model_outputs, return_timestamps=None, return_language=None):
        # unpack the outputs from list(dict(list)) to list(dict)
        model_outputs = [dict(zip(output, t)) for output in model_outputs for t in zip(*output.values())]

        time_precision = self.feature_extractor.chunk_length / self.model.config.max_source_positions
        # Send the chunking back to seconds, it's easier to handle in whisper
        sampling_rate = self.feature_extractor.sampling_rate
        for output in model_outputs:
            if "stride" in output:
                chunk_len, stride_left, stride_right = output["stride"]
                # Go back in seconds
                chunk_len /= sampling_rate
                stride_left /= sampling_rate
                stride_right /= sampling_rate
                output["stride"] = chunk_len, stride_left, stride_right

        text, optional = self.tokenizer._decode_asr(
            model_outputs,
            return_timestamps=return_timestamps,
            return_language=return_language,
            time_precision=time_precision,
        )
        return {"text": text, **optional}

    def forward(
        self,
        model_inputs,
        batch_size=None,
        language=None,
        task=None,
        return_timestamps=False,
        num_beams=3,
        length_penalty=1.0,
        do_sample=False,
        top_k=50,
        temperature=1.0,
    ):
        # We need to keep track of some additional input arguments for post-processing so need to forward these on after running generation
        input_features = model_inputs.pop("input_features")
        input_batch_size = input_features.shape[0]

        if input_batch_size != batch_size:
            padding = np.zeros([batch_size - input_batch_size, *input_features.shape[1:]], input_features.dtype)
            input_features = np.concatenate([input_features, padding])

        pred_ids = self.generate(
            input_features,
            language=language,
            task=task,
            return_timestamps=return_timestamps,
            num_beams=num_beams,
            length_penalty=length_penalty,
            do_sample=do_sample,
            top_k=top_k,
            temperature=temperature,
        )[:input_batch_size]

        # tokenizer's decode method expects an extra dim - we insert it here for convenience
        out = {"tokens": pred_ids[:, None, :]}

        stride = model_inputs.pop("stride", None)
        if stride is not None:
            out["stride"] = stride

        return out

    def __call__(
        self,
        inputs,
        chunk_length_s=30.0,
        stride_length_s=None,
        batch_size=None,
        language=None,
        task=None,
        return_timestamps=None,
        num_beams=1,
        length_penalty=1.0,
        do_sample=False,
        top_k=50,
        temperature=1.0,
    ):
        """
        Transcribe an audio input sequence to a text transcription, optionally with timestamps.

        Args:
            inputs (`np.ndarray` or `bytes` or `str` or `dict`):
                The inputs is either:
                    - `str` that is the filename of the audio file, the file will be read at the correct sampling rate
                      to get the waveform using *ffmpeg*. This requires *ffmpeg* to be installed on the system.
                    - `bytes` is the byte content of an audio file and is interpreted by *ffmpeg* in the
                      same way.
                    - (`np.ndarray` of shape (n, ) of type `np.float32` or `np.float64`)
                        Raw audio assumed to be at the correct sampling rate (16kHz). Note that no further sampling
                        rate check will be done.
                    - `dict` form can be used to pass raw audio sampled at arbitrary `sampling_rate` and let this
                      pipeline do the resampling. The dict must be in the format `{"sampling_rate": int, "array":
                      np.array}`. Optionally an additional argument `"stride": (left: int, right: int)` can be used to
                       ask the pipeline to treat the first `left` samples and last `right` samples to be ignored in
                       decoding (but used at inference to provide more context to the model). In general, this additional
                       stride argument is not required.
            chunk_length_s (`float`, *optional*, defaults to 30.0):
                The input length for each chunk. If `chunk_length_s = 0` then chunking is disabled. By default, the chunk
                length is set 30.0s, equal to Whisper's context window.
            stride_length_s (`float`, *optional*, defaults to `chunk_length_s / 6`):
                The length of stride on the left and right of each chunk. Used only with `chunk_length_s > 0`. This enables
                the model to *see* more context and infer letters better than without this context but the pipeline
                discards the stride bits at the end to make the final reconstitution as perfect as possible.

                <Tip>

                For more information on how to effectively use `stride_length_s`, refer to the [ASR chunking
                blog post](https://huggingface.co/blog/asr-chunking).

                </Tip>
            batch_size (`int`, *optional*, defaults to the minimum per-device batch size, i.e. `jax.local_device_count()`):
                The batch size to be used in chunking transcription. Beneficial for transcribing long audio files. Passing
                a batch size in the `__call__` method will supersede any batch size passed to the `__init__`.
            task (`str`, *optional*):
                Task to use for generation, either `"transcribe"` or `"translate"`. Defaults to `"transcribe"`.
            language (`str`, *optional*):
                Language token to use for generation, can be either in the form of `"<|en|>"`, `"en"` or `"english"`.
                Defaults to `None`, meaning the language is automatically inferred from the audio input.
            return_timestamps (*optional*, `bool`):
                Whether to return timestamps in the prediction. Defaults to False. If set to true, the pipeline
                will return two keys in the output dictionary: `"text"` containing the text transcription, and `"chunks"`
                containing the transcription segments chunked by their utterance-level timestamps.
            length_penalty (*optional*, `float`):
                Exponential penalty to the length that is used with beam-based generation. It is applied as an
                exponent to the sequence length, which in turn is used to divide the score of the sequence. Since
                the score is the log likelihood of the sequence (i.e. negative), length_penalty > 1.0 promotes
                longer sequences, while length_penalty < 1.0 encourages shorter sequences.
            do_sample (*optional*, `bool`):
                Whether or not to use sampling ; use greedy decoding otherwise.
            top_k (*optional*, `int`):
                The number of the highest probability vocabulary tokens to keep for top-k-filtering.
            temperature (*optional*, `float`):
                The value used to modulate the next token probabilities if sampling.

        Return:
            `Dict`: A dictionary with the following keys:
                - **text** (`str` ) -- The recognised text.
                - **chunks** (*optional(, `List[Dict]`)
                    When using `return_timestamps`, the `chunks` will become a list containing all the various text
                    chunks identified by the model, *e.g.* `[{"text": "hi ", "timestamps": (0.5,0.9), {"text":
                    "there", "timestamps": (1.0, 1.5)}]`. The original full text can roughly be recovered by doing
                    `"".join(chunk["text"] for chunk in output["chunks"])`.
        """
        batch_size = batch_size if batch_size is not None else self.batch_size
        if batch_size % self.min_batch_size != 0:
            raise ValueError(
                f"Batch size must be a multiple of the number of JAX devices, but got batch size {batch_size} and num devices {self.min_batch_size}."
            )

        dataloader = self.preprocess_batch(
            inputs, chunk_length_s=chunk_length_s, stride_length_s=stride_length_s, batch_size=batch_size
        )
        model_outputs = []
        # iterate over our chunked audio samples
        for batch in dataloader:
            model_outputs.append(
                self.forward(
                    batch,
                    batch_size=batch_size,
                    language=language,
                    task=task,
                    return_timestamps=return_timestamps,
                    num_beams=num_beams,
                    length_penalty=length_penalty,
                    do_sample=do_sample,
                    top_k=top_k,
                    temperature=temperature,
                )
            )
        post_processed = self.postprocess(model_outputs, return_timestamps=return_timestamps)
        return post_processed
