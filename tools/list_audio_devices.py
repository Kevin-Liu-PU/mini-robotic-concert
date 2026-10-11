"""List output-capable devices without creating an input or output stream."""
import json


def main():
    import sounddevice as sd
    devices = sd.query_devices()
    hosts = sd.query_hostapis()
    outputs = [dict(index=i, name=d['name'], host_api=hosts[d['hostapi']]['name'],
                    output_channels=d['max_output_channels'])
               for i,d in enumerate(devices) if d['max_output_channels'] > 0]
    print(json.dumps(outputs, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
