# Taken straight from Patter https://github.com/ryanleary/patter
# TODO: review, and copyright and fix/add comments
import random
import os
import io
import webdataset as wd
import torch
import braceexpand
import librosa
import numpy as np
from scipy import signal
from itertools import cycle
from torch.utils.data import IterableDataset

from nemo import logging
from nemo.collections.asr.parts import collections, parsers
from nemo.collections.asr.parts.segment import AudioSegment

try:
    from nemo.collections.asr.parts import numba_utils

    HAVE_NUMBA = True
except (ImportError, ModuleNotFoundError):
    HAVE_NUMBA = False

def read_one_audiosegment(manifest, target_sr, rng, tarred_audio=False, audiodata=None,
                         orig_sr=None, noise_data=None):
    if tarred_audio:
        audio_segment = rng.sample(noise_data, 1)[0]
        return audio_segment
        # audio_file, file_id = next(audiodata)
        # manifest_idx = manifest.mapping[file_id]
        # manifest_entry = manifest[manifest_idx]
        #
        # offset = manifest_entry.offset
        # logging.debug("audio file: %s", file_id)
        # if offset is None:
        #     offset = 0
    else:
        audio_record = rng.sample(manifest.data, 1)[0]
        audio_file = audio_record.audio_file
        offset = audio_record.offset
        return AudioSegment.from_file(audio_file, target_sr=target_sr, offset=offset, downsample_rate=orig_sr)

class Perturbation(object):
    def max_augmentation_length(self, length):
        return length

    def perturb(self, data, orig_sr=None):
            raise NotImplementedError



class SpeedPerturbation(Perturbation):
    def __init__(self, sr, resample_type, min_speed_rate=0.9, max_speed_rate=1.1, num_rates=5, rng=None):
        """
        Performs Speed Augmentation by re-sampling the data to a different sampling rate,
        which does not preserve pitch.

        Note: This is a very slow operation for online augmentation. If space allows,
        it is preferable to pre-compute and save the files to augment the dataset.

        Args:
            sr: Original sampling rate.
            resample_type: Type of resampling operation that will be performed.
                For better speed using `resampy`'s fast resampling method, use `resample_type='kaiser_fast'`.
                For high-quality resampling, set `resample_type='kaiser_best'`.
                To use `scipy.signal.resample`, set `resample_type='fft'` or `resample_type='scipy'`
            min_speed_rate: Minimum sampling rate modifier.
            max_speed_rate: Maximum sampling rate modifier.
            num_rates: Number of discrete rates to allow. Can be a positive or negative
                integer.
                If a positive integer greater than 0 is provided, the range of
                speed rates will be discretized into `num_rates` values.
                If a negative integer or 0 is provided, the full range of speed rates
                will be sampled uniformly.
                Note: If a positive integer is provided and the resultant discretized
                range of rates contains the value '1.0', then those samples with rate=1.0,
                will not be augmented at all and simply skipped. This is to unnecessary
                augmentation and increase computation time. Effective augmentation chance
                in such a case is = `prob * (num_rates - 1 / num_rates) * 100`% chance
                where `prob` is the global probability of a sample being augmented.
            rng: Random seed number.
        """
        min_rate = min(min_speed_rate, max_speed_rate)
        if min_rate < 0.0:
            raise ValueError("Minimum sampling rate modifier must be > 0.")

        if resample_type not in ('kaiser_best', 'kaiser_fast', 'fft', 'scipy'):
            raise ValueError("Supported `resample_type` values are ('kaiser_best', 'kaiser_fast', 'fft', 'scipy')")

        self._sr = sr
        self._min_rate = min_speed_rate
        self._max_rate = max_speed_rate
        self._num_rates = num_rates
        if num_rates > 0:
            self._rates = np.linspace(self._min_rate, self._max_rate, self._num_rates, endpoint=True)
        self._res_type = resample_type
        self._rng = random.Random() if rng is None else rng

    def max_augmentation_length(self, length):
        return length * self._max_rate

    def perturb(self, data, orig_sr=None):
        # Select speed rate either from choice or random sample
        if self._num_rates < 0:
            speed_rate = self._rng.uniform(self._min_rate, self._max_rate)
        else:
            speed_rate = self._rng.choice(self._rates)

        # Skip perturbation in case of identity speed rate
        if speed_rate == 1.0:
            return

        new_sr = int(self._sr * speed_rate)
        data._samples = librosa.core.resample(data._samples, self._sr, new_sr, res_type=self._res_type)


class TimeStretchPerturbation(Perturbation):
    def __init__(self, min_speed_rate=0.9, max_speed_rate=1.1, num_rates=5, n_fft=512, rng=None):
        """
        Time-stretch an audio series by a fixed rate while preserving pitch, based on [1, 2].

        Note:
        This is a simplified implementation, intended primarily for reference and pedagogical purposes.
        It makes no attempt to handle transients, and is likely to produce audible artifacts.

        Reference
        [1] [Ellis, D. P. W. “A phase vocoder in Matlab.” Columbia University, 2002.]
        (http://www.ee.columbia.edu/~dpwe/resources/matlab/pvoc/)
        [2] [librosa.effects.time_stretch]
        (https://librosa.github.io/librosa/generated/librosa.effects.time_stretch.html)

        Args:
            min_speed_rate: Minimum sampling rate modifier.
            max_speed_rate: Maximum sampling rate modifier.
            num_rates: Number of discrete rates to allow. Can be a positive or negative
                integer.
                If a positive integer greater than 0 is provided, the range of
                speed rates will be discretized into `num_rates` values.
                If a negative integer or 0 is provided, the full range of speed rates
                will be sampled uniformly.
                Note: If a positive integer is provided and the resultant discretized
                range of rates contains the value '1.0', then those samples with rate=1.0,
                will not be augmented at all and simply skipped. This is to avoid unnecessary
                augmentation and increase computation time. Effective augmentation chance
                in such a case is = `prob * (num_rates - 1 / num_rates) * 100`% chance
                where `prob` is the global probability of a sample being augmented.
            n_fft: Number of fft filters to be computed.
            rng: Random seed number.
        """
        min_rate = min(min_speed_rate, max_speed_rate)
        if min_rate < 0.0:
            raise ValueError("Minimum sampling rate modifier must be > 0.")

        self._min_rate = min_speed_rate
        self._max_rate = max_speed_rate
        self._num_rates = num_rates
        if num_rates > 0:
            self._rates = np.linspace(self._min_rate, self._max_rate, self._num_rates, endpoint=True)
        self._rng = random.Random() if rng is None else rng

        # Pre-compute constants
        self._n_fft = int(n_fft)
        self._hop_length = int(n_fft // 2)

        # Pre-allocate buffers
        self._phi_advance_fast = np.linspace(0, np.pi * self._hop_length, self._hop_length + 1)
        self._scale_buffer_fast = np.empty(self._hop_length + 1, dtype=np.float32)

        self._phi_advance_slow = np.linspace(0, np.pi * self._n_fft, self._n_fft + 1)
        self._scale_buffer_slow = np.empty(self._n_fft + 1, dtype=np.float32)

    def max_augmentation_length(self, length):
        return length * self._max_rate

    def perturb(self, data, orig_sr=None):
        # Select speed rate either from choice or random sample
        if self._num_rates < 0:
            speed_rate = self._rng.uniform(self._min_rate, self._max_rate)
        else:
            speed_rate = self._rng.choice(self._rates)

        # Skip perturbation in case of identity speed rate
        if speed_rate == 1.0:
            return

        # Increase `n_fft` based on task (speed up or slow down audio)
        # This greatly reduces upper bound of maximum time taken
        # to compute slowed down audio segments.
        if speed_rate >= 1.0:  # Speed up audio
            fft_multiplier = 1
            phi_advance = self._phi_advance_fast
            scale_buffer = self._scale_buffer_fast

        else:  # Slow down audio
            fft_multiplier = 2
            phi_advance = self._phi_advance_slow
            scale_buffer = self._scale_buffer_slow

        n_fft = int(self._n_fft * fft_multiplier)
        hop_length = int(self._hop_length * fft_multiplier)

        # Perform short-term Fourier transform (STFT)
        stft = librosa.core.stft(data._samples, n_fft=n_fft, hop_length=hop_length)

        # Stretch by phase vocoding
        if HAVE_NUMBA:
            stft_stretch = numba_utils.phase_vocoder(stft, speed_rate, phi_advance, scale_buffer)

        else:
            stft_stretch = librosa.core.phase_vocoder(stft, speed_rate, hop_length)

        # Predict the length of y_stretch
        len_stretch = int(round(len(data._samples) / speed_rate))

        # Invert the STFT
        y_stretch = librosa.core.istft(
            stft_stretch, dtype=data._samples.dtype, hop_length=hop_length, length=len_stretch
        )

        data._samples = y_stretch


class GainPerturbation(Perturbation):
    def __init__(self, min_gain_dbfs=-10, max_gain_dbfs=10, rng=None):
        self._min_gain_dbfs = min_gain_dbfs
        self._max_gain_dbfs = max_gain_dbfs
        self._rng = random.Random() if rng is None else rng

    def perturb(self, data, orig_sr=None):
        gain = self._rng.uniform(self._min_gain_dbfs, self._max_gain_dbfs)
        # logging.debug("gain: %d", gain)
        data._samples = data._samples * (10.0 ** (gain / 20.0))


class ImpulsePerturbation(Perturbation):
    def __init__(self, manifest_path=None, rng=None, audio_tar_filepaths=None, shuffle_n=100):
        self._manifest = collections.ASRAudioText(manifest_path, parser=parsers.make_parser([]),
                                                  index_by_file_id=True)
        self._audiodataset = None
        self._tarred_audio = False

        if audio_tar_filepaths:
            self._tarred_audio = True
            self._audiodataset = AugmentationDataset(manifest_path, audio_tar_filepaths, shuffle_n=shuffle_n)
            self._noise_list = []
            self._data_iterator = iter(self._audiodataset)


        self._rng = random.Random() if rng is None else rng

    def perturb(self, data, orig_sr=None):
        if not self._noise_list:
            self._noise_list = self._audiodataset.get_all_data(data.sample_rate)
        impulse = read_one_audiosegment(self._manifest, data.sample_rate, self._rng, tarred_audio=self._tarred_audio,
                                        audiodata=self._data_iterator, orig_sr=orig_sr, noise_data=self._noise_list)
        #impulse_norm = (impulse.samples - min(impulse.samples)) / (max(impulse.samples) - min(impulse.samples))
        max_ind = np.argmax(impulse.samples)

        impulse_resp = impulse.samples[max_ind:]
        delay_after = len(impulse_resp)
        data._samples = signal.fftconvolve(data._samples, impulse_resp, "full")[:-delay_after]


class ShiftPerturbation(Perturbation):
    def __init__(self, min_shift_ms=-5.0, max_shift_ms=5.0, rng=None):
        self._min_shift_ms = min_shift_ms
        self._max_shift_ms = max_shift_ms
        self._rng = random.Random() if rng is None else rng

    def perturb(self, data, orig_sr = None):
        shift_ms = self._rng.uniform(self._min_shift_ms, self._max_shift_ms)
        if abs(shift_ms) / 1000 > data.duration:
            # TODO: do something smarter than just ignore this condition
            return
        shift_samples = int(shift_ms * data.sample_rate // 1000)
        # logging.debug("shift: %s", shift_samples)
        if shift_samples < 0:
            data._samples[-shift_samples:] = data._samples[:shift_samples]
            data._samples[:-shift_samples] = 0
        elif shift_samples > 0:
            data._samples[:-shift_samples] = data._samples[shift_samples:]
            data._samples[-shift_samples:] = 0


class RirAndNoisePerturbation(Perturbation):
    def __init__(
        self, rir_manifest_path=None, noise_manifest_path=None, min_snr_db=0, max_snr_db=40,
        max_gain_db=300.0, rng=None, rir_tar_filepaths=None, rir_shuffle_n=100,
        noise_tar_filepaths=None, noise_shuffle_n=100, apply_noise_rir=False,
        max_additions=1, max_duration=5.0,
        bg_noise_manifest_path=None, bg_noise_tar_filepaths=None, bg_noise_shuffle_n=100,
        bg_min_snr_db=10, bg_max_snr_db = 40, bg_max_gain_db=300.0,

    ):
        logging.info("Called init")
        self._rir_perturber = ImpulsePerturbation(manifest_path=rir_manifest_path, rng=rng,
                                                   audio_tar_filepaths=rir_tar_filepaths, shuffle_n=rir_shuffle_n)
        self._fg_noise_perturber = NoisePerturbation(manifest_path=noise_manifest_path, min_snr_db=min_snr_db,
                                                    max_snr_db=max_snr_db, max_gain_db=max_gain_db, rng=rng,
                                                    audio_tar_filepaths=noise_tar_filepaths, shuffle_n=noise_shuffle_n)
        if bg_noise_manifest_path:
            self._bg_noise_perturber = NoisePerturbation(manifest_path=bg_noise_manifest_path, min_snr_db=bg_min_snr_db,
                                                         max_snr_db=bg_max_snr_db, max_gain_db=bg_max_gain_db, rng=rng,
                                                         audio_tar_filepaths=bg_noise_tar_filepaths, shuffle_n=bg_noise_shuffle_n)
        else:
            self._bg_noise_perturber = None
        self._rng = random.Random() if rng is None else rng
        self._max_additions = max_additions
        self._max_duration = max_duration

        self._apply_noise_rir = apply_noise_rir

    def perturb(self, data, orig_sr=None):
        self._rir_perturber.perturb(data)
        data_rms = data.rms_db
        noise = self._fg_noise_perturber.get_one_noise_sample(data.sample_rate, orig_sr)


        if self._apply_noise_rir:
            self._rir_perturber.perturb(noise)
        self._fg_noise_perturber.perturb_with_point_noise(data, noise, data_rms=data_rms,
                                                          max_noise_dur=self._max_duration,
                                                          max_additions=self._max_additions)
        if self._bg_noise_perturber:
            self._bg_noise_perturber.perturb(data, orig_sr, data_rms)

class NoisePerturbation(Perturbation):
    def __init__(
        self, manifest_path=None, min_snr_db=40, max_snr_db=50, max_gain_db=300.0, rng=None,
        audio_tar_filepaths = None, shuffle_n = 100,
    ):
        self._manifest = collections.ASRAudioText(manifest_path, parser=parsers.make_parser([]),
                                                  index_by_file_id=True)
        self._audiodataset = None
        self._tarred_audio = False

        if audio_tar_filepaths:
            self._tarred_audio = True
            self._audiodataset = AugmentationDataset(manifest_path, audio_tar_filepaths, shuffle_n=shuffle_n)
            self._noise_list = []
            self._data_iterator = iter(self._audiodataset)

        self._rng = random.Random() if rng is None else rng
        self._min_snr_db = min_snr_db
        self._max_snr_db = max_snr_db
        self._max_gain_db = max_gain_db

    def get_one_noise_sample(self, target_sr, orig_sr=None):
        if not self._noise_list:
            self._noise_list = self._audiodataset.get_all_data(target_sr, orig_sr=orig_sr)
        return read_one_audiosegment(self._manifest, target_sr, self._rng, tarred_audio=self._tarred_audio,
                              audiodata=self._data_iterator, orig_sr=orig_sr, noise_data=self._noise_list)

    def perturb(self, data, orig_sr=None, data_rms=None):
        if not self._noise_list:
            self._noise_list = self._audiodataset.get_all_data(data.sample_rate, orig_sr=orig_sr)
        noise = read_one_audiosegment(self._manifest, data.sample_rate, self._rng, tarred_audio=self._tarred_audio,
                                      audiodata=self._data_iterator, orig_sr=orig_sr, noise_data=self._noise_list)
        self.perturb_with_input_noise(data, noise, data_rms=data_rms)

    def perturb_with_point_noise(self, data, noise, data_rms=None, max_noise_dur=5, max_additions=1,):
        snr_db = self._rng.uniform(self._min_snr_db, self._max_snr_db)
        if not data_rms:
            data_rms = data.rms_db

        #if data.duration < max_noise_dur:
        #    return
        noise_gain_db = min(data_rms - noise.rms_db - snr_db, self._max_gain_db)
        # adjust gain for snr purposes and superimpose
        #noise.gain_db(noise_gain_db)
        # logging.debug("noise: %s %s %s", snr_db, noise_gain_db, noise_record.audio_file)
        n_additions = self._rng.randint(1,max_additions)
        for i in range(n_additions):
            noise_dur = self._rng.uniform(0.0, max_noise_dur)
            start_time = self._rng.uniform(0.0, noise.duration)
            start_sample = int(round(start_time * noise.sample_rate))
            end_sample = int(round( min(noise.duration, (start_time + noise_dur)) * noise.sample_rate))
            noise_samples = np.copy(noise._samples[start_sample:end_sample])
            # adjust gain for snr purposes and superimpose
            noise_samples *= 10.0 ** (noise_gain_db / 20.0)

            if noise_samples.shape[0] > data._samples.shape[0]:
                noise_samples = noise_samples[0:data._samples.shape[0]]


            logging.debug("data dur: %f, data shape:%d, noise dur:%f noise shape:%d",  data.duration, data._samples.shape[0],
                            noise_dur, noise._samples.shape[0])
            noise_idx = self._rng.randint(0, data._samples.shape[0] - noise_samples.shape[0])
            data._samples[noise_idx : noise_idx + noise_samples.shape[0]] += noise_samples

    def perturb_with_input_noise(self, data, noise, data_rms=None):

        snr_db = self._rng.uniform(self._min_snr_db, self._max_snr_db)
        if not data_rms:
            data_rms = data.rms_db

        noise_gain_db = min(data_rms - noise.rms_db - snr_db, self._max_gain_db)
        # logging.debug("noise: %s %s %s", snr_db, noise_gain_db, noise_record.audio_file)

        # calculate noise segment to use
        start_time = max(0, self._rng.uniform(0.0, noise.duration - data.duration))
        start_sample = int(round(start_time * noise.sample_rate))
        end_sample = int(round(min(noise.duration, (start_time + data.duration)) * noise.sample_rate))
        noise_samples = np.copy(noise._samples[start_sample:end_sample])
        #if noise.duration > (start_time + data.duration):
        #    noise.subsegment(start_time=start_time, end_time=start_time + data.duration)

        # adjust gain for snr purposes and superimpose
        noise_samples *= 10.0 ** (noise_gain_db / 20.0)
        #noise.gain_db(noise_gain_db)
        data._samples[0: noise_samples.shape[0]] += noise_samples

        # if noise_samples.shape[0] < data._samples.shape[0]:
        #     noise_idx = self._rng.randint(0, data._samples.shape[0] - noise._samples.shape[0])
        #     data._samples[noise_idx : noise_idx + noise._samples.shape[0]] += noise._samples
        #
        # else:
        #     data._samples += noise_samples

class WhiteNoisePerturbation(Perturbation):
    def __init__(self, min_level=-90, max_level=-46, rng=None):
        self.min_level = int(min_level)
        self.max_level = int(max_level)
        self._rng = np.random.RandomState() if rng is None else rng

    def perturb(self, data, orig_sr=None):
        noise_level_db = self._rng.randint(self.min_level, self.max_level, dtype='int32')
        noise_signal = self._rng.randn(data._samples.shape[0]) * (10.0 ** (noise_level_db / 20.0))
        data._samples += noise_signal


perturbation_types = {
    "speed": SpeedPerturbation,
    "time_stretch": TimeStretchPerturbation,
    "gain": GainPerturbation,
    "impulse": ImpulsePerturbation,
    "shift": ShiftPerturbation,
    "noise": NoisePerturbation,
    "white_noise": WhiteNoisePerturbation,
    "rir_noise": RirAndNoisePerturbation,
}


def register_perturbation(name: str, perturbation: Perturbation):
    if name in perturbation_types.keys():
        raise KeyError(
            f"Perturbation with the name {name} exists. " f"Type of perturbation : {perturbation_types[name]}."
        )

    perturbation_types[name] = perturbation


class AudioAugmentor(object):
    def __init__(self, perturbations=None, rng=None):
        self._rng = random.Random() if rng is None else rng
        self._pipeline = perturbations if perturbations is not None else []

    def perturb(self, segment, orig_sr=None):
        for (prob, p) in self._pipeline:
            if self._rng.random() < prob:
                p.perturb(segment, orig_sr)
        return

    def max_augmentation_length(self, length):
        newlen = length
        for (prob, p) in self._pipeline:
            newlen = p.max_augmentation_length(newlen)
        return newlen

    @classmethod
    def from_config(cls, config):
        ptbs = []
        for p in config:
            if p['aug_type'] not in perturbation_types:
                logging.warning("%s perturbation not known. Skipping.", p['aug_type'])
                continue
            perturbation = perturbation_types[p['aug_type']]
            ptbs.append((p['prob'], perturbation(**p['cfg'])))
        return cls(perturbations=ptbs)

class AugmentationDataset(IterableDataset):
    """Change the actual and nominal length of an IterableDataset.

    :param dataset: IterableDataset
    :param length: declared length of the dataset
    :param nominal: nominal length of dataset (if different from declared)

    This will continuously iterate through the original dataset, but
    impose new epoch boundaries at the given length/nominal.
    This exists mainly as a workaround for the odd logic in DataLoader.
    It is also useful for choosing smaller nominal epoch sizes with
    very large datasets.

    """

    def __init__(self, manifest_path, audio_tar_filepaths, n_tars_per_worker=1, shuffle_n=100):
        self._manifest = collections.ASRAudioText(manifest_path, parser=parsers.make_parser([]),
                                                      index_by_file_id=True)

        if torch.distributed.is_available() and torch.distributed.is_initialized():
            global_rank = torch.distributed.get_rank()
            world_size = torch.distributed.get_world_size()
            if isinstance(audio_tar_filepaths, str):
                audio_tar_filepaths = list(braceexpand.braceexpand(audio_tar_filepaths))

            if len(audio_tar_filepaths) % world_size != 0:
                logging.warning(
                    f"Number of shards in tarred dataset ({len(audio_tar_filepaths)}) is not divisible "
                    f"by number of distributed workers ({world_size})."
                )

            #if n_tars_per_worker * world_size < len(audio_tar_filepaths):
            logging.warning(
                f"global rank {global_rank} worldsize {world_size}Number of shards in tarred dataset ({len(audio_tar_filepaths)}) is greater than "
                f"number of tars per worker ({n_tars_per_worker}) * number of distributed workers ({world_size})."
            )

            tar_list = []
            for i in range(n_tars_per_worker):
                tar_list.append(audio_tar_filepaths[((global_rank*n_tars_per_worker) + i )% len(audio_tar_filepaths)])
            audio_tar_filepaths = tar_list

        self._noise_list = []

        self.audio_dataset = (
            wd.Dataset(audio_tar_filepaths).shuffle(shuffle_n).rename(audio='wav', key='__key__')
                .to_tuple('audio', 'key').pipe(self._filter))
        self.audio_iter = iter(self.audio_dataset)

    def _filter(self, iterator):
        """Used to remove samples that have been filtered out by ASRAudioText already.
        Otherwise, we would get a KeyError as _build_sample attempts to find the manifest entry for a sample
        that was filtered out (e.g. for duration).
        """

        class TarredAudioFilter:
            def __init__(self, collection):
                self.iterator = iterator
                self.collection = collection

            def __iter__(self):
                return self

            def __next__(self):
                while True:
                    audio_bytes, audio_filename = next(self.iterator)
                    file_id, _ = os.path.splitext(os.path.basename(audio_filename))
                    if file_id in self.collection.mapping:
                        return audio_bytes, audio_filename

        return TarredAudioFilter(self._manifest)

    def __len__(self):
        return len(self._manifest)

    def __iter__(self):
        audio_bytes, audio_filename=None,None
        while True:
            try:
                audio_bytes, audio_filename = next(self.audio_iter)

            except StopIteration:
                self.audio_iter = iter(self.audio_dataset)
                audio_bytes, audio_filename = next(self.audio_iter)
                # Grab manifest entry from self.collection
            file_id, _ = os.path.splitext(os.path.basename(audio_filename))

            # Convert audio bytes to IO stream for processing (for SoundFile to read)
            audio_file = io.BytesIO(audio_bytes)
            sample = (audio_file, file_id)
            yield sample

    def get_iter(self):
        return self.audio_iter

    def get_all_data(self, target_sr, orig_sr=None):
        if not self._noise_list:
            while True:
                try:
                    audio_bytes, audio_filename = next(self.audio_iter)

                except StopIteration:
                    break
                file_id, _ = os.path.splitext(os.path.basename(audio_filename))

                # Convert audio bytes to IO stream for processing (for SoundFile to read)
                audio_file = io.BytesIO(audio_bytes)
                sample = (audio_file, file_id)
                self._noise_list.append(AudioSegment.from_file(audio_file, target_sr=target_sr, downsample_rate=orig_sr, read_only=True))
                if self._noise_list[-1].rms_db == float("-inf"):
                    logging.info("0 file found: %s", file_id )
        return self._noise_list

# class RirAndNoisePerturbation2(Perturbation):
#     def __init__(
#         self, rir_manifest_path=None, noise_manifest_path=None, min_snr_db=10, max_snr_db=50,
#         max_gain_db=300.0, rng=None, rir_tar_filepaths=None, rir_shuffle_n=100,
#         noise_tar_filepaths=None, noise_shuffle_n=100,
#     ):
#         logging.info("Called init")
#         self._rir_manifest = collections.ASRAudioText(rir_manifest_path, parser=parsers.make_parser([]),
#                                                       index_by_file_id=True)
#         self._noise_manifest = collections.ASRAudioText(noise_manifest_path, parser=parsers.make_parser([]),
#                                                         index_by_file_id=True)
#         self.noise_audiodataset = None
#         self.rir_audiodataset = None
#         self.rir_tarred_audio = False
#         self.noise_tarred_audio = False
#         if rir_tar_filepaths:
#             self.rir_tarred_audio = True
#             self.rir_audiodataset = AugmentationDataset(rir_manifest_path, rir_tar_filepaths)
#             self.rir_data = iter(self.rir_audiodataset)
#         if rir_tar_filepaths:
#             self.noise_tarred_audio = True
#             self.noise_audiodataset = AugmentationDataset(noise_manifest_path, noise_tar_filepaths)
#             self.noise_data = iter(self.noise_audiodataset)
#
#         self._rng = random.Random() if rng is None else rng
#         self._min_snr_db = min_snr_db
#         self._max_snr_db = max_snr_db
#         self._max_gain_db = max_gain_db

    # def perturb(self, data, orig_sr=None):
    #
    #     impulse = self.read_one_audiosegment(self._rir_manifest, data.sample_rate, tarred_audio=self.rir_tarred_audio,
    #                                         audiodata=self.rir_data, orig_sr=orig_sr)
    #     max_ind = np.argmax(impulse.samples)
    #
    #     impulse_resp = impulse.samples[max_ind:]
    #     delay_after = len(impulse_resp)
    #
    #     data._samples = signal.fftconvolve(data._samples, impulse_resp, "full")[:-delay_after]
    #
    #     snr_db = self._rng.uniform(self._min_snr_db, self._max_snr_db)
    #     noise = self.read_one_audiosegment(self._noise_manifest, data.sample_rate, tarred_audio=self.rir_tarred_audio,
    #                                       audiodata=self.noise_data, orig_sr=orig_sr)
    #
    #     logging.debug("noise file: num_samples=%d, sample_rate=%d, duration=%.2fsec",
    #                   noise.num_samples, noise.sample_rate, noise.duration)
    #
    #     datarms = data.rms_db
    #     logging.debug("called data rms = %.10f", datarms)
    #     noiserms = noise.rms_db
    #     logging.debug("called noise rms =%.10f",noiserms)
    #     noise_gain_db = min(datarms - noiserms - snr_db, self._max_gain_db)
    #     logging.debug("noise file: num_samples=%d, sample_rate=%d, duration=%.2fsec rms1=%.10f rms2=%.10f",
    #                   noise.num_samples, noise.sample_rate, noise.duration,datarms,noiserms )
    #
    #
    #     # logging.debug("noise: %s %s %s", snr_db, noise_gain_db, noise_record.audio_file)
    #
    #     # calculate noise segment to use
    #     start_time = self._rng.uniform(0.0, noise.duration - data.duration)
    #     if noise.duration > (start_time + data.duration):
    #         noise.subsegment(start_time=start_time, end_time=start_time + data.duration)
    #
    #     # adjust gain for snr purposes and superimpose
    #     noise.gain_db(noise_gain_db)
    #
    #     if noise._samples.shape[0] < data._samples.shape[0]:
    #         noise_idx = self._rng.randint(0, data._samples.shape[0] - noise._samples.shape[0])
    #         data._samples[noise_idx : noise_idx + noise._samples.shape[0]] += noise._samples
    #
    #     else:
    #         data._samples += noise._samples
