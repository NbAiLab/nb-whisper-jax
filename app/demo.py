import logging
import math
import os
import shutil
import tempfile
import time
from multiprocessing import Pool

import gradio as gr
import jax.numpy as jnp
import numpy as np
import yt_dlp as youtube_dl
from jax.experimental.compilation_cache import compilation_cache as cc
from pydub import AudioSegment
from transformers.models.whisper.tokenization_whisper import TO_LANGUAGE_CODE
from transformers.pipelines.audio_utils import ffmpeg_read
import tempfile
import base64
import subprocess
from typing import Tuple
import locale

from whisper_jax import FlaxWhisperPipeline

cc.initialize_cache("./jax_cache")

import argparse
import re

# Define valid checkpoints and corresponding batch sizes
valid_checkpoints = {
    "tiny": 128,
    "base": 128,
    "small": 256,
    "medium": 32,
    "large": 8,
}

# Create the parser
parser = argparse.ArgumentParser(description='Run the transcription script with a specific checkpoint.')
parser.add_argument('--checkpoint', type=str, help='The checkpoint to use for the model.', required=True)

# Parse the arguments
args = parser.parse_args()

# Check if the checkpoint is valid
found_batch_size = None
for keyword, batch_size in valid_checkpoints.items():
    if keyword in args.checkpoint.lower():
        found_batch_size = batch_size
        break

if found_batch_size is None:
    print(f"Error: The specified checkpoint is not supported.")
    exit(1)

# If the checkpoint is valid, set it and the corresponding batch size
checkpoint = args.checkpoint
BATCH_SIZE = found_batch_size

# Generate title from the checkpoint name
title_parts = checkpoint.split("/")
title = title_parts[-1]  # Take the part after the slash
title = title.replace("-", " ").lower()  # Replace hyphens with spaces

title = title.title()
title = title.replace("Nb Whisper", "NB-Whisper")
title = title.replace("Beta", "(beta)")
title = title.replace("Rc", "RC")

NUM_PROC = 32
FILE_LIMIT_MB = 1000
YT_LENGTH_LIMIT_S = 10800  # limit to 3 hour YouTube files

description = ""

article = """
<div style='text-align: center;'>
Submit feedback <a href='https://forms.gle/cCQzdox9N2ENDczV7'>here</a>. Backend running JAX on a TPU v3 through support from the 
<a href='https://sites.research.google/trc/about/'>TRC</a> programme. 
Whisper JAX <a href='https://github.com/sanchit-gandhi/whisper-jax'>code</a> and Gradio demo by 🤗 Hugging Face.
</div>
"""


language_names = sorted(TO_LANGUAGE_CODE.keys())

logger = logging.getLogger("whisper-jax-app")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s;%(levelname)s;%(message)s", "%Y-%m-%d %H:%M:%S")
ch.setFormatter(formatter)
logger.addHandler(ch)


def convert_to_proper_time_format(time_str):
    """
    Converts time string to proper VTT time format. Returns a default format if input is None.
    """
    if time_str is None or time_str == "None":
        logging.warning("Received None for time_str in convert_to_proper_time_format.")
        return "99:00:00.000"

    if len(time_str) == 8:
        return time_str + ".000"
    elif len(time_str) > 8:
        return time_str
    else:
        raise ValueError(f"Invalid time format: {time_str}")


# Updated format_to_vtt function
def format_to_vtt(text, timestamps, transcription_style, style=""):
    if not timestamps:
        return None

    # Set style based on transcription type
    if transcription_style == "verbatim":
        style = "line:20% align:center position:50% size:80%"
    elif transcription_style == "semantic":
        style = "line:80% align:center position:50% size:80%"

    vtt_lines = [
        f"WEBVTT",
        "",
        "NOTE",
        f"Denne transkripsjonen er autogenerert av Nasjonalbibliotekets {title} basert på OpenAIs Whisper-modell.",
        f"Se detaljer og last ned modellen her: https://huggingface.co/{checkpoint}.",
        "",
        "0",
        ""
    ]
    # Removed
    #         f"00:00:00.000 --> 00:00:06.000 {style}".strip(),
    #     f"(Automatisk teksting av {title})",
    
    counter = 1
    for chunk in text.split("\n"):
        try:
            start_time, rest = chunk.split(" -> ")
            end_time, subtitle_text = rest.split("] ")
        except ValueError:
            logging.warning(f"Skipping malformed chunk: {chunk}")
            continue

        start_time = start_time.replace("[", "").replace(",", ".")
        end_time = end_time.replace(",", ".")

        start_time = convert_to_proper_time_format(start_time)
        end_time = convert_to_proper_time_format(end_time)

        if end_time is None:
            logging.warning(f"End time is None for chunk: {chunk}")
            continue

        # Don't let the disclaimer overlap with the first subtitle
        if start_time.startswith("00:00:0") and int(start_time[7]) < 6:
            vtt_lines[7] = vtt_lines[7].replace("00:00:06.000", start_time)

        subtitle_text = subtitle_text.strip()
        subtitle_text = subtitle_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        subtitle_text = split_long_lines(subtitle_text)

        vtt_lines.append(str(counter))
        vtt_lines.append(f"{start_time} --> {end_time} {style}".strip())
        vtt_lines.append(subtitle_text)
        vtt_lines.append("")

        counter += 1

    return "\n".join(vtt_lines)


# Updated merge_and_sort_subtitles function
def merge_and_sort_subtitles(vtt_file1, vtt_file2):
    def extract_subtitles(vtt_file):
        with open(vtt_file, 'r') as file:
            lines = file.readlines()

        # Find the index where actual subtitles start
        start_index = 0
        for i, line in enumerate(lines):
            if "-->" in line:
                start_index = i - 1
                break

        # Extract subtitles
        subtitles = []
        current_subtitle = []
        for line in lines[start_index:]:
            if line.strip().isdigit() and current_subtitle:
                subtitles.append(current_subtitle)
                current_subtitle = [line]
            else:
                current_subtitle.append(line)
        if current_subtitle:
            subtitles.append(current_subtitle)

        return subtitles, start_index

    # Extract subtitles from both files
    subtitles1, start_index1 = extract_subtitles(vtt_file1)
    subtitles2, _ = extract_subtitles(vtt_file2)

    # Merge subtitles without sorting as overlapping is allowed
    merged_subtitles = subtitles1 + subtitles2

    # Update numbering for merged subtitles
    for idx, group in enumerate(merged_subtitles, start=1):
        group[0] = f"{idx}\n"

    # Read header from the first file
    with open(vtt_file1, 'r') as file:
        header = ''.join(file.readlines()[:start_index1])

    # Combine header and merged subtitle groups
    combined_vtt = header + ''.join([''.join(group) for group in merged_subtitles])

    # Save subtitles to a temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix='.vtt')
    with os.fdopen(temp_fd, 'w') as temp_file:
        for subtitle in combined_vtt:
            temp_file.writelines(subtitle)

    return temp_path


def split_long_lines(text, max_length=75):
    """
    Splits long lines into shorter lines of specified maximum length, ensuring spaces are preserved.
    """
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        # Check if adding the next word exceeds the max length
        if len(current_line) + len(word) + 1 <= max_length:
            # Append the word with a space, or no space if it's the first word in the line
            current_line = f"{current_line} {word}".strip()
        else:
            # Line has reached max length, start a new line
            lines.append(current_line)
            current_line = word

    # Add the last line if it's not empty
    if current_line:
        lines.append(current_line)

    return '\n'.join(lines)


def identity(batch):
    return batch


# Modified from https://github.com/openai/whisper/blob/c09a7ae299c4c34c5839a76380ae407e7d785914/whisper/utils.py#L50
def format_timestamp(seconds: float, always_include_hours: bool = True, decimal_marker: str = "."):
    if seconds is not None:
        milliseconds = round(seconds * 1000.0)

        hours = milliseconds // 3_600_000
        milliseconds -= hours * 3_600_000

        minutes = milliseconds // 60_000
        milliseconds -= minutes * 60_000

        seconds = milliseconds // 1_000
        milliseconds -= seconds * 1_000

        hours_marker = f"{hours:02d}:"
        return f"{hours_marker}{minutes:02d}:{seconds:02d}{decimal_marker}{milliseconds:03d}"
    else:
        # we have a malformed timestamp so just return it as is
        return seconds


if __name__ == "__main__":
    pipeline = FlaxWhisperPipeline(checkpoint, dtype=jnp.bfloat16, batch_size=BATCH_SIZE)
    pool = Pool(NUM_PROC)

    # do a pre-compile step so that the first user to use the demo isn't hit with a long transcription time
    logger.info("compiling forward call...")
    start = time.time()
    # random_inputs = {"input_features": np.ones((BATCH_SIZE, 80, 3000))}
    
    random_inputs = {"input_features": np.ones(
            (BATCH_SIZE, pipeline.model.config.num_mel_bins, 2 * pipeline.model.config.max_source_positions)
        )
    }
    
    random_timestamps = pipeline.forward(random_inputs, batch_size=BATCH_SIZE, return_timestamps=True)
    compile_time = time.time() - start
    logger.info(f"compiled in {compile_time}s")


    def tqdm_generate(inputs: dict, language: str, task: str, return_timestamps: bool, chunk_length_s: int, num_beams: int,length_penalty: bool, top_k: int, temperature: bool, progress: gr.Progress) -> Tuple[
        str, float]:
        stride_length_s = chunk_length_s / 6
        chunk_len = round(chunk_length_s * pipeline.feature_extractor.sampling_rate)
        stride_left = stride_right = round(stride_length_s * pipeline.feature_extractor.sampling_rate)
        step = chunk_len - stride_left - stride_right
       
        inputs_len = inputs["array"].shape[0]
        all_chunk_start_idx = np.arange(0, inputs_len, step)
        num_samples = len(all_chunk_start_idx)
        num_batches = math.ceil(num_samples / BATCH_SIZE)
        dummy_batches = list(range(num_batches))  # Gradio progress bar not compatible with generator

        dataloader = pipeline.preprocess_batch(inputs, chunk_length_s=chunk_length_s, batch_size=BATCH_SIZE)
        progress(0, desc="Pre-processing audio file...")
        logger.info("pre-processing audio file...")
        dataloader = pool.map(identity, dataloader)
        logger.info("done post-processing")

        if language == "Bokmål":
            language = "<|no|>"
        elif language == "Nynorsk":
            language = "<|nn|>"
        else:
            language = "<|en|>"

        start_time = time.time()
        verbatim_outputs = []
        semantic_outputs = []

        # Verbatim (transcribe) loop
        if task in ["Verbatim", "Compare"]:
            for batch, _ in zip(dataloader, progress.tqdm(dummy_batches, desc="Transcribing...")):
                if temperature != 1.0 or top_k != 50:
                    do_sample = True
                else:
                    do_sample = False

                if num_beams == 1:
                    # Can't use length penalty without beam search
                    length_penalty = 1.0
                else:
                    # Beam sampling is not implemented in FlaxWhisperPipeline
                    temperature = 1.0
                    top_k = 50

                # Ensure num_beams and top_k are integers
                num_beams = int(num_beams)
                top_k = int(top_k)

                # Ensure length_penalty and temperature are positive floats
                length_penalty = max(0.0, float(length_penalty))
                temperature = max(0.0, float(temperature))

                logger.info(f"Transcribing task: {task}, language: {language}, return_timestamps: {return_timestamps}, chunk_length_s: {chunk_length_s}, num_beams: {num_beams}, length_penalty: {length_penalty}, top_k: {top_k}, temperature: {temperature}")

                verbatim_outputs.append(
                    pipeline.forward(batch, batch_size=BATCH_SIZE, task="transcribe", language=language,
                                     num_beams=num_beams,length_penalty=length_penalty, top_k=top_k, temperature=temperature, do_sample=do_sample,return_timestamps=return_timestamps)
                )

        # Semantic (translate) loop
        if task in ["Semantic", "Compare"]:
            for batch, _ in zip(dataloader, progress.tqdm(dummy_batches, desc="Translating...")):
                semantic_outputs.append(
                    pipeline.forward(batch, batch_size=BATCH_SIZE, task="translate", language=language,
                                     return_timestamps=return_timestamps)
                )

        runtime = time.time() - start_time
        logger.info("done with tasks")
        logger.info("post-processing...")

        # Post-process and combine results
        combined_text = ""
        combined_timestamps = []

        if task in ["Verbatim", "Compare"]:
            verbatim_post_processed = pipeline.postprocess(verbatim_outputs, return_timestamps=return_timestamps)
            verbatim_text = verbatim_post_processed["text"]
            if return_timestamps:
                combined_timestamps.extend(verbatim_post_processed.get("chunks", []))
            combined_text += verbatim_text

        if task in ["Semantic", "Compare"]:
            semantic_post_processed = pipeline.postprocess(semantic_outputs, return_timestamps=return_timestamps)
            semantic_text = semantic_post_processed["text"]
            if return_timestamps:
                combined_timestamps.extend(semantic_post_processed.get("chunks", []))
            combined_text += semantic_text

        if return_timestamps:
            timestamps_text = [
                f"[{format_timestamp(chunk['timestamp'][0])} -> {format_timestamp(chunk['timestamp'][1])}] {chunk['text']}"
                for chunk in combined_timestamps
            ]
            combined_text = "\n".join(timestamps_text)

        logger.info(
            f"Processed {len(combined_text.split())} words and {len(combined_text)} characters in {runtime:.2f}s")
        return combined_text.strip(), runtime


    def prepare_audio_for_transcription(file):
        tmpdirname = tempfile.mkdtemp()
        file_path = os.path.join(tmpdirname, file)
        shutil.move(file, file_path)
        file_size_mb = os.stat(file_path).st_size / (1024 * 1024)
        if file_size_mb > FILE_LIMIT_MB:
            raise Exception(f"File size exceeds limit: {file_size_mb:.2f}MB / {FILE_LIMIT_MB}MB")

        if file_path.endswith(".mp4"):
            video = AudioSegment.from_file(file_path, "mp4")
            audio_path_pydub = file_path.replace(".mp4", ".wav")
            video.export(audio_path_pydub, format="wav")
            with open(audio_path_pydub, "rb") as f:
                file_contents = f.read()
        else:
            with open(file_path, "rb") as f:
                file_contents = f.read()
            video_file_path = re.sub(r"\.[^.]+$", ".mp4", file_path)
            ffmpeg_cmd = f'ffmpeg -y -f lavfi -i color=c=black:s=1280x720 -i "{file_path}" ' \
                         f'-shortest -fflags +shortest -loglevel error "{video_file_path}"'
            os.system(ffmpeg_cmd)
            file_path = video_file_path

        return file_contents, file_path


    def create_transcript_file(text, file_path, return_timestamps, transcription_style="semantic"):
        if return_timestamps:
            # Formatting for middle-aligned subtitles
            transcript_content = format_to_vtt(text, return_timestamps, transcription_style=None,
                                               style="line:50% align:center")
            subtitle_display = re.sub(r"\.[^.]+$", "_middle.vtt", file_path)
            with open(subtitle_display, "w") as f:
                f.write(transcript_content)

            # Formatting for regular subtitles with transcription style
            transcript_content = format_to_vtt(text, return_timestamps, transcription_style=transcription_style)
            transcript_file_path = re.sub(r"\.[^.]+$", f"_{transcription_style}.vtt", file_path)
        else:
            # Handling non-timestamped text
            transcript_content = text
            transcript_file_path = re.sub(r"\.[^.]+$", f"_{transcription_style}.txt", file_path)
            subtitle_display = None

        with open(transcript_file_path, "w") as f:
            f.write(transcript_content)

        return transcript_file_path, subtitle_display


    def perform_transcription(file_contents, language, task, return_timestamps, chunk_length_s, num_beams,length_penalty, top_k, temperature, progress):
        inputs = ffmpeg_read(file_contents, pipeline.feature_extractor.sampling_rate)
        inputs = {"array": inputs, "sampling_rate": pipeline.feature_extractor.sampling_rate}
        logger.info("done loading")
        
        text, runtime = tqdm_generate(inputs, language=language, task=task, return_timestamps=return_timestamps, chunk_length_s=chunk_length_s, num_beams=num_beams,length_penalty=length_penalty, top_k=top_k, temperature=temperature,
                                      progress=progress)
        return text, runtime



    def transcribe_chunked_audio(file_or_yt_url, language="Bokmål", return_timestamps=True, chunk_length_slider=28, num_beams_slider=1, length_penalty_slider=1.0, top_k_slider=50, temperature_slider=1.0, progress=gr.Progress()):
        locale.setlocale(locale.LC_ALL, '')
        task = "Verbatim"
        stats = {}

        if not file_or_yt_url:
            raise gr.Error("No input provided. Please provide a file or a YouTube URL.")

        # Handling different input types
        download_start_time = time.time()
        if isinstance(file_or_yt_url, str) and file_or_yt_url.startswith("http"):
            # Handle YouTube URL input
            yt_url = file_or_yt_url
            progress(0, desc="Loading YouTube audio...")
            logger.info("loading YouTube audio...")
            tmpdirname = tempfile.mkdtemp()
            video_filepath = download_yt_audio(yt_url, tmpdirname, video=return_timestamps)
            file_contents, file_path = prepare_audio_for_transcription(video_filepath)
            download_time = time.time() - download_start_time
        else:
            # Handle file upload or microphone input
            file_contents, file_path = prepare_audio_for_transcription(file_or_yt_url if hasattr(file_or_yt_url, 'name') else file_or_yt_url)
            download_time = 0.0  # No download time for non-YouTube inputs

        stats['download_time'] = f"{download_time:.1f}"

        # Preprocessing audio file
        preprocessing_start_time = time.time()
        audio = AudioSegment.from_file(file_path)
        preprocessing_time = time.time() - preprocessing_start_time
        stats['preprocessing_time'] = f"{preprocessing_time:.1f}"

        # Audio length in seconds
        audio_length = len(audio) / 1000.0  # Convert milliseconds to seconds
        stats['audio_length'] = f"{audio_length:.1f}"

        # Perform transcription
        transcription_start_time = time.time()
        text, runtime = perform_transcription(file_contents, language, task, return_timestamps, chunk_length_slider, num_beams_slider, length_penalty_slider, top_k_slider, temperature_slider, progress)
        transcription_time = time.time() - transcription_start_time
        stats['transcription_time'] = f"{transcription_time:.1f}"  # Add transcription time to stats

        # Handle timestamps in transcription text and create transcript file
        if return_timestamps:
            transcript_file_path, subtitle_display = create_transcript_file(text, file_path, return_timestamps, transcription_style=task)
            clean_text = re.sub(r"\[\d{2}:\d{2}:\d{2}\.\d{3} -> \d{2}:\d{2}:\d{2}\.\d{3}\] ", "", text)
        else:
            transcript_file_path = None
            subtitle_display = None
            clean_text = text

        word_count = len(clean_text.split())
        char_count = len(clean_text)
        stats['word_count'] = f"{word_count:,}"

        # Calculate and format transcription speed
        if transcription_time > 0:
            speed = round(audio_length / transcription_time)
            stats['speed'] = f"{speed}x"
        else:
            stats['speed'] = "N/A"

        # Convert all stats to strings with thousand separators
        for key, value in stats.items():
            if isinstance(value, float):
                stats[key] = f"{value:,.1f}"
            elif isinstance(value, int):
                stats[key] = f"{value:,}"

        # Update video and audio components based on the file type
        video_output = [file_path, subtitle_display] if file_path.endswith(".mp4") and subtitle_display else file_path
        audio_output = file_path if not file_path.endswith(".mp4") else None

        # Format stats as a Markdown table
        stats_md = "|Audio Length|Words|Download|Pre-processing|Transcription|Speed|\n"
        stats_md += "|:---:|:---:|:---:|:---:|:---:|:---:|\n"  # Updated table column alignment with three hyphens

        # Convert second-based measures to string with 's' appended
        for key in ['download_time', 'preprocessing_time', 'audio_length', 'transcription_time']:
            stats[key] = f"{stats[key]}s" if stats[key] != "N/A" else stats[key]

        stats_md += f"|{stats['audio_length']}|{stats['word_count']}|{stats['download_time']}|{stats['preprocessing_time']}|{stats['transcription_time']}|{stats['speed']}|"


        # Return the outputs along with the stats as a string (for debugging)
        return video_output, audio_output, text, str(stats_md), transcript_file_path



    def download_yt_audio(yt_url, folder, video=False):
        info_loader = youtube_dl.YoutubeDL()
        try:
            info = info_loader.extract_info(yt_url, download=False)
        except youtube_dl.utils.DownloadError as err:
            raise gr.Error(str(err))

        file_length = info["duration_string"]
        file_h_m_s = file_length.split(":")
        file_h_m_s = [int(sub_length) for sub_length in file_h_m_s]
        if len(file_h_m_s) == 1:
            file_h_m_s.insert(0, 0)
        if len(file_h_m_s) == 2:
            file_h_m_s.insert(0, 0)

        file_length_s = file_h_m_s[0] * 3600 + file_h_m_s[1] * 60 + file_h_m_s[2]
        if file_length_s > YT_LENGTH_LIMIT_S:
            yt_length_limit_hms = time.strftime("%HH:%MM:%SS", time.gmtime(YT_LENGTH_LIMIT_S))
            file_length_hms = time.strftime("%HH:%MM:%SS", time.gmtime(file_length_s))
            raise gr.Error(f"Maximum YouTube length is {yt_length_limit_hms}, got {file_length_hms} YouTube video.")

        fpath = os.path.join(folder, f"{info['id'].replace('.', '_')}.mp4")

        video = "bestvideo[height <=? 480]" if video else "worstvideo"
        ydl_opts = {"outtmpl": fpath, "format": f"{video}[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"}
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            try:
                ydl.download([yt_url])
            except youtube_dl.utils.ExtractorError as err:
                raise gr.Error(str(err))

        # Return ID
        return fpath


def clear(audio, language, timestamps_checkbox, chunk_length_s, num_beams, length_penalty, top_k, temperature, transcription):
    # Reset all fields to their default values
    return None, "Bokmål", True, 28, 1, 1.0, 50, 1.0, ""

def update_sliders(num_beams):
    # Update length_penalty_slider
    length_penalty_update = gr.update(visible=False, value=1.0) if num_beams == 1 else gr.update(visible=True)
    
    # Update top_k_slider and temperature_slider
    sliders_visibility = False if num_beams > 1 else True
    top_k_update = gr.update(visible=sliders_visibility, value=50)
    temperature_update = gr.update(visible=sliders_visibility, value=1.0)

    return length_penalty_update, top_k_update, temperature_update


youtube_examples=[
    ["https://www.youtube.com/watch?v=_uv74o8hG30", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=YcBWSBRuk0Q", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=vauTloX4HkU", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=WHF74ppqKFQ", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=b8nz4sh_sj4", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=vauTloX4HkU", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=pMesxWW-daA", "Bokmål", "Verbatim", True, False],
    ["https://www.youtube.com/watch?v=x0Fsn4I54C0", "Bokmål", "Verbatim", True, False]
    
]
with gr.Blocks() as demo:
    gr.Image("NB-logo-eng-farge.png", show_label=False, height=100, interactive=False, container=False)
    gr.Markdown(f"<h1 style='text-align: center;color: #C10A26'>{title}</h1>")
    with gr.Tab("Audio"):
        with gr.Row():
            with gr.Column():
                # Inputs and buttons for "Audio" tab
                audio_input = gr.Audio(sources=["upload", "microphone"], label="Audio file", type="filepath")
                language_input = gr.Radio(["Bokmål", "Nynorsk", "English"], label="Output Language", value="Bokmål")
                timestamps_checkbox = gr.Checkbox(value=True, label="Return timestamps")

                with gr.Accordion(label="Advanced Options", open=False):
                    chunk_length_slider = gr.Slider(minimum=10, maximum=30, step=1, label="Chunk Length", value=28)
                    num_beams_slider = gr.Slider(minimum=1, maximum=10, step=1, label="Number of Beams", value=1)
                    length_penalty_slider = gr.Slider(minimum=0.1, maximum=2.0, step=0.1, label="Length Penalty", value=1.0, visible=False)
                    top_k_slider = gr.Slider(minimum=1, maximum=100, step=1, label="Top K", value=50)
                    temperature_slider = gr.Slider(minimum=0.0, maximum=2.0, step=0.1, label="Temperature", value=1.0)

                    # Update sliders based on num_beams_slider value
                    num_beams_slider.change(update_sliders, inputs=num_beams_slider, outputs=[length_penalty_slider, top_k_slider, temperature_slider])


                with gr.Row():
                    clear_button = gr.Button("Clear")
                    submit_button = gr.Button("Submit", variant="primary")

            with gr.Column():
                video_output = gr.Video(label="Video", visible=True)
                audio_output = gr.Audio(label="Audio", visible=False)
                transcription_output = gr.Textbox(label="Transcription", show_copy_button=True, show_label=True)
                with gr.Accordion(label="Statistics", open=True):
                    stats_output = gr.Markdown()
                download_output = gr.File(label="Download")

            clear_button.click(
                clear,
                inputs=[audio_input, language_input, timestamps_checkbox, chunk_length_slider, num_beams_slider, length_penalty_slider, top_k_slider, temperature_slider, transcription_output],
                outputs=[audio_input, language_input, timestamps_checkbox, chunk_length_slider, num_beams_slider, length_penalty_slider, top_k_slider, temperature_slider, transcription_output]
            )
            submit_button.click(
                transcribe_chunked_audio,
                inputs=[audio_input, language_input, timestamps_checkbox, chunk_length_slider, num_beams_slider, length_penalty_slider, top_k_slider, temperature_slider],
                outputs=[video_output,audio_output,transcription_output,stats_output,download_output]
            )

    with gr.Tab("YouTube"):
        with gr.Row():
            with gr.Column():
                # Inputs and buttons for "Video" tab
                yt_input = gr.Textbox(lines=1, placeholder="Paste the URL to a YouTube or Twitter/X video here", label="YouTube or Twitter/X URL")
                yt_language_input = gr.Radio(["Bokmål", "Nynorsk", "English"], label="Output Language", value="Bokmål")
                yt_timestamps_checkbox = gr.Checkbox(value=True, label="Return timestamps")
                
                with gr.Accordion(label="Advanced Options", open=False):
                    chunk_length_slider2 = gr.Slider(minimum=10, maximum=30, step=1, label="Chunk Length", value=28)
                    num_beams_slider2 = gr.Slider(minimum=1, maximum=10, step=1, label="Number of Beams", value=1)
                    length_penalty_slider2 = gr.Slider(minimum=0.1, maximum=2.0, step=0.1, label="Length Penalty", value=1.0, visible=False)
                    top_k_slider2 = gr.Slider(minimum=1, maximum=100, step=1, label="Top K", value=50)
                    temperature_slider2 = gr.Slider(minimum=0.0, maximum=2.0, step=0.1, label="Temperature", value=1.0)

                    # Update sliders based on num_beams_slider2 value for YouTube tab
                    num_beams_slider2.change(update_sliders, inputs=num_beams_slider2, outputs=[length_penalty_slider2, top_k_slider2, temperature_slider2])


                with gr.Row():
                    clear_button2 = gr.Button("Clear")
                    submit_button2 = gr.Button("Submit", variant="primary")
                    
                #Add examples for YouTube tab
                gr.Examples(
                    examples=youtube_examples,
                    inputs=[yt_input, yt_language_input, yt_timestamps_checkbox],
                    cache_examples=False
                )

            with gr.Column():
                yt_video_output = gr.Video(label="Video")
                yt_audio_output = gr.Audio(label="Audio", visible=False)
                yt_transcription_output = gr.Textbox(label="Transcription", show_copy_button=True, show_label=True)
                with gr.Accordion(label="Statistics", open=True):
                    yt_stats_output = gr.Markdown()
                yt_download_output = gr.File(label="Download")



            clear_button2.click(
                clear,
                inputs=[yt_input, yt_language_input, yt_timestamps_checkbox,chunk_length_slider2, num_beams_slider2, length_penalty_slider2, top_k_slider2, temperature_slider2, yt_transcription_output],
                outputs=[yt_input, yt_language_input, yt_timestamps_checkbox, chunk_length_slider2, num_beams_slider2, length_penalty_slider2, top_k_slider2, temperature_slider2, yt_transcription_output]
            )
            submit_button2.click(
                transcribe_chunked_audio,
                inputs=[yt_input, yt_language_input, yt_timestamps_checkbox, chunk_length_slider2,num_beams_slider2, length_penalty_slider2, top_k_slider2, temperature_slider2],
                outputs=[yt_video_output,yt_audio_output,yt_transcription_output,yt_stats_output,yt_download_output]
            )

    gr.Markdown(article)
    

demo.queue(max_size=10)
demo.launch()
