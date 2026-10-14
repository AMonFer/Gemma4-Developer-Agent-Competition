import json
import os
import struct
import sys
from pathlib import Path

def prune_safetensors(src_path: Path, dst_path: Path):
    print(f"Reading: {src_path}")
    with open(src_path, "rb") as f:
        header_size = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(header_size).decode("utf-8"))
        data_start = 8 + header_size

        new_header = {}
        if "__metadata__" in header:
            new_header["__metadata__"] = header["__metadata__"]

        tensors_to_copy = []
        current_offset = 0

        for k, v in header.items():
            if k == "__metadata__":
                continue
            # Keep language model tensors, drop vision tower tensors
            if "language_model" in k:
                old_start, old_end = v["data_offsets"]
                size = old_end - old_start
                new_v = dict(v)
                new_v["data_offsets"] = [current_offset, current_offset + size]
                new_header[k] = new_v
                tensors_to_copy.append((old_start, old_end, size))
                current_offset += size

        header_bytes = json.dumps(new_header, separators=(",", ":")).encode("utf-8")
        pad = (8 - (len(header_bytes) % 8)) % 8
        header_bytes += b" " * pad
        new_header_size = len(header_bytes)

        print(f"Writing pruned weights to: {dst_path}")
        with open(dst_path, "wb") as out_f:
            out_f.write(struct.pack("<Q", new_header_size))
            out_f.write(header_bytes)

            for old_start, old_end, size in tensors_to_copy:
                f.seek(data_start + old_start)
                chunk = f.read(size)
                out_f.write(chunk)

    old_mb = os.path.getsize(src_path) / (1024 * 1024)
    new_mb = os.path.getsize(dst_path) / (1024 * 1024)
    tensor_count = len([k for k in new_header if k != "__metadata__"])
    print(f"Pruning complete:")
    print(f"  - Original size: {old_mb:.2f} MB")
    print(f"  - Pruned size:   {new_mb:.2f} MB")
    print(f"  - Retained tensors: {tensor_count} (pure language_model)")

if __name__ == "__main__":
    src = Path("src/adapters/lead_lora_v1/adapter_model.safetensors")
    dst = Path("src/adapters/lead_lora_v1/adapter_model_pruned.safetensors")
    prune_safetensors(src, dst)
