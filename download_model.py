"""Explicit one-time download from Google's official MediaPipe model bucket."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
DEST = Path(__file__).resolve().parent / "models" / "hand_landmarker.task"
SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEST,
                        help='Destination for the verified model; defaults to models/hand_landmarker.task')
    destination = parser.parse_args().output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(".download")
        try:
            with urllib.request.urlopen(URL, timeout=60) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if temporary.stat().st_size < 1000000:
                raise RuntimeError("Model download is unexpectedly small")
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
                raise RuntimeError("Downloaded model failed the pinned SHA256 check")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("Existing model failed the pinned SHA256 check; move it aside and download again")
    print(f"Model: {destination}")
    print(f"SHA256: {hashlib.sha256(destination.read_bytes()).hexdigest()}")
    print(f"Source: {URL}")


if __name__ == "__main__":
    main()
