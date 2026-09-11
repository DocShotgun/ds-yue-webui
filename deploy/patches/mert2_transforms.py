"""Vendored torchaudio transforms (Spectrogram, MelScale, AmplitudeToDB).

A faithful port of torchaudio's transforms. These three classes are unchanged
across torchaudio 2.8 through 2.11 and remain in torchaudio 2.11+; this module
exists only for environments where torchaudio cannot be installed for the chosen
torch build. The apply script patches SheetSage2's

    from torchaudio.transforms import AmplitudeToDB, MelScale, Spectrogram

into an import of this module (copied alongside the patched code). Numerics:
the Spectrogram delegates to torch.stft exactly like torchaudio.functional.spectrogram,
and the mel filterbank uses torchaudio.functional.melscale_fbanks' formulas
(htk scale, norm=None, which is what SheetSage2's MERT2 frontend uses).

Note: this module does NOT cover torchaudio.load/info/decode (removed upstream in
2.9); SheetSage2's "paper" preset needs those. Use the "default" preset (FFmpeg
file decoding) when running with this patch.
"""
from __future__ import annotations

import math
import warnings

import torch
from torch import Tensor
from torch import nn

__all__ = ["Spectrogram", "MelScale", "AmplitudeToDB"]


def _spectrogram(waveform: Tensor, pad: int, window: Tensor, n_fft: int, hop_length: int,
                 win_length: int, power: float | None, normalized: bool | str,
                 center: bool = True, pad_mode: str = "reflect", onesided: bool = True) -> Tensor:
    if pad > 0:
        waveform = torch.nn.functional.pad(waveform, (pad, pad), "constant")
    frame_length_norm, window_norm = False, False
    if isinstance(normalized, str):
        if normalized not in ("frame_length", "window"):
            raise ValueError(f"Invalid normalized parameter: {normalized}")
        frame_length_norm, window_norm = normalized == "frame_length", normalized == "window"
    elif isinstance(normalized, bool):
        window_norm = normalized
    else:
        raise TypeError("Input type not supported")
    shape = waveform.size()
    waveform = waveform.reshape(-1, shape[-1])
    spec_f = torch.stft(
        input=waveform, n_fft=n_fft, hop_length=hop_length, win_length=win_length,
        window=window, center=center, pad_mode=pad_mode, normalized=frame_length_norm,
        onesided=onesided, return_complex=True)
    spec_f = spec_f.reshape(shape[:-1] + spec_f.shape[-2:])
    if window_norm:
        spec_f /= window.pow(2.0).sum().sqrt()
    if power is not None:
        return spec_f.abs() if power == 1.0 else spec_f.abs().pow(power)
    return spec_f


def _amplitude_to_DB(x: Tensor, multiplier: float, amin: float, db_multiplier: float,
                     top_db: float | None = None) -> Tensor:
    x_db = multiplier * torch.log10(torch.clamp(x, min=amin))
    x_db -= multiplier * db_multiplier
    if top_db is not None:
        shape = x_db.size()
        packed_channels = shape[-3] if x_db.dim() > 2 else 1
        x_db = x_db.reshape(-1, packed_channels, shape[-2], shape[-1])
        x_db = torch.max(x_db, (x_db.amax(dim=(-3, -2, -1)) - top_db).view(-1, 1, 1, 1))
        x_db = x_db.reshape(shape)
    return x_db


def _hz_to_mel(freq: float, mel_scale: str = "htk") -> float:
    if mel_scale not in ("slaney", "htk"):
        raise ValueError('mel_scale should be one of "htk" or "slaney".')
    if mel_scale == "htk":
        return 2595.0 * math.log10(1.0 + (freq / 700.0))
    f_min = 0.0
    f_sp = 200.0 / 3
    mels = (freq - f_min) / f_sp
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = math.log(6.4) / 27.0
    if freq >= min_log_hz:
        mels = min_log_mel + math.log(freq / min_log_hz) / logstep
    return mels


def _mel_to_hz(mels: Tensor, mel_scale: str = "htk") -> Tensor:
    if mel_scale not in ("slaney", "htk"):
        raise ValueError('mel_scale should be one of "htk" or "slaney".')
    if mel_scale == "htk":
        return 700.0 * (10.0 ** (mels / 2595.0) - 1.0)
    f_min = 0.0
    f_sp = 200.0 / 3
    freqs = f_min + f_sp * mels
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = math.log(6.4) / 27.0
    log_t = mels >= min_log_mel
    freqs[log_t] = min_log_hz * torch.exp(logstep * (mels[log_t] - min_log_mel))
    return freqs


def _create_triangular_filterbank(all_freqs: Tensor, f_pts: Tensor) -> Tensor:
    # Adopted from Librosa, as in torchaudio.functional._create_triangular_filterbank.
    f_diff = f_pts[1:] - f_pts[:-1]
    slopes = f_pts.unsqueeze(0) - all_freqs.unsqueeze(1)
    zero = torch.zeros(1)
    down_slopes = (-1.0 * slopes[:, :-2]) / f_diff[:-1]
    up_slopes = slopes[:, 2:] / f_diff[1:]
    return torch.max(zero, torch.min(down_slopes, up_slopes))


def _melscale_fbanks(n_freqs: int, f_min: float, f_max: float, n_mels: int, sample_rate: int,
                     norm: str | None = None, mel_scale: str = "htk") -> Tensor:
    if norm is not None and norm != "slaney":
        raise ValueError('norm must be one of None or "slaney"')
    all_freqs = torch.linspace(0, sample_rate // 2, n_freqs)
    m_min = _hz_to_mel(f_min, mel_scale=mel_scale)
    m_max = _hz_to_mel(f_max, mel_scale=mel_scale)
    m_pts = torch.linspace(m_min, m_max, n_mels + 2)
    f_pts = _mel_to_hz(m_pts, mel_scale=mel_scale)
    fb = _create_triangular_filterbank(all_freqs, f_pts)
    if norm is not None and norm == "slaney":
        enorm = 2.0 / (f_pts[2:n_mels + 2] - f_pts[:n_mels])
        fb *= enorm.unsqueeze(0)
    if (fb.max(dim=0).values == 0.0).any():
        warnings.warn(
            "At least one mel filterbank has all zero values. "
            f"The value for `n_mels` ({n_mels}) may be set too high. "
            f"Or, the value for `n_freqs` ({n_freqs}) may be set too low.")
    return fb


class Spectrogram(nn.Module):
    __constants__ = ["n_fft", "win_length", "hop_length", "pad", "power", "normalized"]

    def __init__(self, n_fft: int = 400, win_length: int | None = None, hop_length: int | None = None,
                 pad: int = 0, window_fn=torch.hann_window, power: float | None = 2.0,
                 normalized: bool | str = False, wkwargs: dict | None = None, center: bool = True,
                 pad_mode: str = "reflect", onesided: bool = True, return_complex: bool | None = None) -> None:
        super().__init__()
        self.n_fft = n_fft
        self.win_length = win_length if win_length is not None else n_fft
        self.hop_length = hop_length if hop_length is not None else self.win_length // 2
        window = window_fn(self.win_length) if wkwargs is None else window_fn(self.win_length, **wkwargs)
        self.register_buffer("window", window)
        self.pad = pad
        self.power = power
        self.normalized = normalized
        self.center = center
        self.pad_mode = pad_mode
        self.onesided = onesided

    def forward(self, waveform: Tensor) -> Tensor:
        return _spectrogram(waveform, self.pad, self.window, self.n_fft, self.hop_length,
                            self.win_length, self.power, self.normalized, self.center,
                            self.pad_mode, self.onesided)


class AmplitudeToDB(nn.Module):
    __constants__ = ["multiplier", "amin", "ref_value", "db_multiplier"]

    def __init__(self, stype: str = "power", top_db: float | None = None) -> None:
        super().__init__()
        self.stype = stype
        if top_db is not None and top_db < 0:
            raise ValueError("top_db must be positive value")
        self.top_db = top_db
        self.multiplier = 10.0 if stype == "power" else 20.0
        self.amin = 1e-10
        self.ref_value = 1.0
        self.db_multiplier = math.log10(max(self.amin, self.ref_value))

    def forward(self, x: Tensor) -> Tensor:
        return _amplitude_to_DB(x, self.multiplier, self.amin, self.db_multiplier, self.top_db)


class MelScale(nn.Module):
    __constants__ = ["n_mels", "sample_rate", "f_min", "f_max", "n_stft"]

    def __init__(self, n_mels: int = 128, sample_rate: int = 16000, f_min: float = 0.0,
                 f_max: float | None = None, n_stft: int = 201, norm: str | None = None,
                 mel_scale: str = "htk") -> None:
        super().__init__()
        self.n_mels = n_mels
        self.sample_rate = sample_rate
        self.f_max = f_max if f_max is not None else float(sample_rate // 2)
        self.f_min = f_min
        self.n_stft = n_stft
        self.norm = norm
        self.mel_scale = mel_scale
        if f_min > self.f_max:
            raise ValueError(f"Require f_min: {f_min} <= f_max: {self.f_max}")
        fb = _melscale_fbanks(self.n_stft, self.f_min, self.f_max, self.n_mels, self.sample_rate,
                              self.norm, self.mel_scale)
        self.register_buffer("fb", fb)

    def forward(self, specgram: Tensor) -> Tensor:
        # torchaudio: (..., time, freq) matmul (freq, n_mels) -> (..., n_mels, time)
        return torch.matmul(specgram.transpose(-1, -2), self.fb).transpose(-1, -2)
