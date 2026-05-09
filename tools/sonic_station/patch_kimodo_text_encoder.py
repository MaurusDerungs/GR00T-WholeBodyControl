#!/usr/bin/env python3
"""Patch installed Kimodo so TEXT_ENCODER_DEVICE survives Kimodo.to(cuda).

Kimodo's CLI loads the LLM2Vec text encoder and then calls `Kimodo.to(device)`,
which recursively calls `text_encoder.to(cuda)`. On 16 GB GPUs this can OOM
because Llama 3 8B plus the motion model do not fit together.

The upstream LLM2VecEncoder constructor already supports TEXT_ENCODER_DEVICE.
This patch extends its `.to(...)` method to keep using that environment value.
"""

from __future__ import annotations

from pathlib import Path

TARGET = Path("model/llm2vec/llm2vec_wrapper.py")

OLD = """    def to(self, device: torch.device):
        self.model = self.model.to(device)
        self._device = str(device) if not isinstance(device, str) else device
        return self
"""

NEW = """    def to(self, device: torch.device):
        env_device = os.environ.get("TEXT_ENCODER_DEVICE")
        if env_device:
            device = env_device
        self.model = self.model.to(device)
        self._device = str(device) if not isinstance(device, str) else device
        return self
"""


def main() -> None:
    try:
        import kimodo
    except ImportError as exc:
        raise SystemExit(f"Kimodo is not importable in this Python environment: {exc}") from exc

    root = Path(kimodo.__file__).resolve().parent
    target = root / TARGET
    text = target.read_text(encoding="utf-8")

    if NEW in text:
        print(f"Kimodo text encoder patch already applied: {target}")
        return
    if OLD not in text:
        raise SystemExit(f"Could not find expected LLM2VecEncoder.to block in {target}")

    target.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print(f"Applied Kimodo text encoder CPU/offload patch: {target}")


if __name__ == "__main__":
    main()
