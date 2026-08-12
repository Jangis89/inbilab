#!/usr/bin/env python3
# Silero VAD helper (onnxruntime, no torch)
import sys, json, wave
import numpy as np
import onnxruntime as ort

MODEL = "/app/models/silero_vad.onnx"
SR = 16000
WINDOW = 512

def _session():
    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = 1
    return ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])

def selfcheck():
    sess = _session()
    ins = [i.name for i in sess.get_inputs()]
    x = np.zeros((1, WINDOW), dtype=np.float32)
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(SR, dtype=np.int64)
    sess.run(None, {"input": x, "state": state, "sr": sr})
    print("VAD_SELFCHECK_OK inputs=" + ",".join(ins))

def read_wav_mono16k(path):
    wf = wave.open(path, "rb")
    raw = wf.readframes(wf.getnframes())
    wf.close()
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

def get_speech_timestamps(audio, threshold=0.5, min_speech_ms=250, min_silence_ms=100, pad_ms=30):
    sess = _session()
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(SR, dtype=np.int64)
    probs = []
    n = len(audio)
    for start in range(0, n, WINDOW):
        chunk = audio[start:start + WINDOW]
        if len(chunk) < WINDOW:
            chunk = np.pad(chunk, (0, WINDOW - len(chunk)))
        x = chunk.reshape(1, -1).astype(np.float32)
        out = sess.run(None, {"input": x, "state": state, "sr": sr})
        state = out[1]
        probs.append(float(np.array(out[0]).reshape(-1)[0]))
    import sys as _s
    if probs:
        _hi = sum(1 for _p in probs if _p >= threshold)
        _lo = sum(1 for _p in probs if _p < 0.35)
        _s.stderr.write("VADDBG n=%d win=%d pmin=%.3f pmax=%.3f pmean=%.3f hi=%d lo=%d" % (n, len(probs), min(probs), max(probs), sum(probs)/len(probs), _hi, _lo) + chr(10))
    neg = max(0.15, threshold - 0.15)
    min_speech = SR * min_speech_ms / 1000.0
    min_silence = SR * min_silence_ms / 1000.0
    pad = int(SR * pad_ms / 1000.0)
    triggered = False
    segs = []
    cur = None
    temp_end = 0
    for i, p in enumerate(probs):
        s = i * WINDOW
        if p >= threshold:
            temp_end = 0
            if not triggered:
                triggered = True
                cur = [s, s]
        elif p < neg and triggered:
            if not temp_end:
                temp_end = s
            if s - temp_end >= min_silence:
                cur[1] = temp_end
                if cur[1] - cur[0] >= min_speech:
                    segs.append(cur)
                cur = None
                triggered = False
                temp_end = 0
    if triggered and cur is not None:
        cur[1] = n
        if cur[1] - cur[0] >= min_speech:
            segs.append(cur)
    out = []
    for seg in segs:
        a = max(0, seg[0] - pad)
        b = min(n, seg[1] + pad)
        if out and a <= out[-1][1]:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [[round(a / SR, 3), round(b / SR, 3)] for a, b in out]

if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        try:
            selfcheck()
            sys.exit(0)
        except Exception as e:
            print("VAD_SELFCHECK_FAIL " + str(e))
            sys.exit(1)
    else:
        path = sys.argv[1]
        min_sil = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        pad = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        audio = read_wav_mono16k(path)
        print(json.dumps({"sr": SR, "speech": get_speech_timestamps(audio, min_silence_ms=min_sil, pad_ms=pad)}))
