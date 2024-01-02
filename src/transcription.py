import traceback
import numpy as np
import openai
import os
import sounddevice as sd
import tempfile
import wave
import webrtcvad
from dotenv import load_dotenv
from faster_whisper import WhisperModel


if load_dotenv():
    openai.api_key = os.getenv('OPENAI_API_KEY')

def process_transcription(transcription, config=None):
    if config:
        if config['remove_trailing_period'] and transcription.endswith('.'):
            transcription = transcription[:-1]
        if config['add_trailing_space']:
            transcription += ' '
        if config['remove_capitalization']:
            transcription = transcription.lower()
    
    return transcription

def create_local_model(config):
    model = WhisperModel(config['local_model_options']['model'],
                         device=config['local_model_options']['device'],
                         compute_type=config['local_model_options']['compute_type'],)
    return model

"""
Record audio from the microphone and transcribe it using the Whisper model.
Recording stops when the user stops speaking.
"""
def record_and_transcribe(status_queue, cancel_flag, config, local_model=None, recording_thread=None):
    sound_device = config['sound_device'] if config else None
    sample_rate = config['sample_rate'] if config else 16000  # 16kHz, supported values: 8kHz, 16kHz, 32kHz, 48kHz, 96kHz
    frame_duration = 30  # 30ms, supported values: 10, 20, 30
    buffer_duration = 300  # 300ms
    silence_duration = config['silence_duration'] if config else 900  # 900ms

    vad = webrtcvad.Vad(3)  # Aggressiveness mode: 3 (highest)
    buffer = []
    recording = []
    num_silent_frames = 0
    num_buffer_frames = buffer_duration // frame_duration
    num_silence_frames = silence_duration // frame_duration
    exit_reason = "Unknown"
    try:
        with sd.InputStream(samplerate=sample_rate, channels=1, dtype='int16', blocksize=sample_rate * frame_duration // 1000,
                            device=sound_device, callback=lambda indata, frames, time, status: buffer.extend(indata[:, 0])) as stream:
            device_info = sd.query_devices(stream.device)
            print('Recording with sound device:', device_info['name']) if config['print_to_terminal'] else ''
            while True:
                if len(buffer) < sample_rate * frame_duration // 1000:
                    continue

                frame = buffer[:sample_rate * frame_duration // 1000]
                buffer = buffer[sample_rate * frame_duration // 1000:]

                is_speech = vad.is_speech(np.array(frame).tobytes(), sample_rate)
                if is_speech:
                    recording.extend(frame)
                    num_silent_frames = 0
                else:
                    if len(recording) > 0:
                        num_silent_frames += 1

                if num_silent_frames >= num_silence_frames or cancel_flag():
                    if len(recording) < sample_rate:  # If <1 sec of audio recorded, continue
                        continue  
                    if cancel_flag():
                        exit_reason= "Hotkey pressed"
                    if num_silent_frames >= num_silence_frames:
                        if recording_thread:
                            recording_thread.stop()
                        exit_reason = "Silence"
                    break

#            if cancel_flag():
#                status_queue.put(('cancel', ''))
#                return ''
        
        audio_data = np.array(recording, dtype=np.int16)
        print(f'Recording finished: {exit_reason}. Size:', audio_data.size) if config['print_to_terminal'] else ''
        
        # Save the recorded audio as a temporary WAV file on disk
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_audio_file:
            with wave.open(temp_audio_file.name, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 2 bytes (16 bits) per sample
                wf.setframerate(sample_rate)
                wf.writeframes(audio_data.tobytes())

        status_queue.put(('transcribing', 'Transcribing...'))
        print('Transcribing audio file...') if config['print_to_terminal'] else ''
        
        # If configured, transcribe the temporary audio file using the OpenAI API
        if config['use_api']:
            api_options = config['api_options']
            with open(temp_audio_file.name, 'rb') as audio_file:
                response = openai.Audio.transcribe(model=api_options['model'], 
                                                   file=audio_file,
                                                   language=api_options['language'],
                                                   prompt=api_options['initial_prompt'],
                                                   temperature=api_options['temperature'],)
            result = response.get('text')
        # Otherwise, transcribe the temporary audio file using a local model
        elif not config['use_api']:
            if not local_model:
                print('Creating local model...') if config['print_to_terminal'] else ''
                local_model = create_local_model(config)
                print('Local model created.') if config['print_to_terminal'] else ''
            model_options = config['local_model_options']
            response = local_model.transcribe(audio=temp_audio_file.name,
                                              language=model_options['language'],
                                              initial_prompt=model_options['initial_prompt'],
                                              condition_on_previous_text=model_options['condition_on_previous_text'],
                                              temperature=model_options['temperature'],
                                              vad_filter=model_options['vad_filter'],)
            result = ''.join([segment.text for segment in list(response[0])])

        # Remove the temporary audio file
        os.remove(temp_audio_file.name)
        
#        if cancel_flag():
#            status_queue.put(('cancel', ''))
#            return ''

        print('Transcription:', result.strip()) if config['print_to_terminal'] else ''
        status_queue.put(('idle', ''))
        
        return process_transcription(result.strip(), config) if result else ''

    except Exception as e:
        traceback.print_exc()
        status_queue.put(('error', 'Error'))

