"""The reader for the multimodal adapter: the only code that sends a screenshot or PDF text to a
model (always through the llm gateway, which caches, logs usage and enforces the caps).

`make_reader(model)` returns reader(file_id, kind, data) -> [read A, read B]. Two reads of the same
file use different prompt framings and different cache variants, so they are independent calls.
Images go as vision input. A PDF is first turned into text with the local `pdftotext`; a PDF with
no text layer raises NoTextLayer (it is listed, never guessed). The model returns facts only.
"""
import subprocess
from pathlib import Path

from . import llm
from .adapters.multimodal import SCHEMA, NoTextLayer

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "screenshot.md"
VARIANTS = ("A", "B")
MAX_TOKENS = 1500
PURPOSE = {"png": "screenshot", "pdf": "screenshot"}  # one purpose, so PDF and image reads share one cap


def load_prompts(path=PROMPT_PATH):
    """{section name: text} from the `=== NAME ===` sections of the prompt file."""
    sections, name, buf = {}, None, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            if name:
                sections[name] = "\n".join(buf).strip()
            name, buf = line[4:-4], []
        else:
            buf.append(line)
    if name:
        sections[name] = "\n".join(buf).strip()
    return sections


def pdf_text(data):
    """Text of a PDF via the local pdftotext (layout kept). NoTextLayer when there is none."""
    try:
        done = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True,
                              timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise NoTextLayer(f"pdftotext failed: {type(e).__name__}") from None
    text = done.stdout.decode("utf-8", "replace").strip()
    if not text:
        raise NoTextLayer("the PDF has no text layer")
    return text


def make_reader(model, prompts=None, text_of=pdf_text):
    prompts = prompts or load_prompts()

    def reader(file_id, kind, data):
        reads = []
        if kind == "png":
            for v in VARIANTS:
                prompt = prompts["COMMON"] + "\n\n" + prompts["IMAGE_" + v]
                reads.append(llm.call(PURPOSE[kind], model, prompt, file_id, SCHEMA,
                                      images=[("image/png", data)], variant=v,
                                      max_tokens=MAX_TOKENS))
        else:
            text = text_of(data)
            for v in VARIANTS:
                prompt = (prompts["COMMON"] + "\n\n"
                          + prompts["PDF_" + v].replace("{document_text}", text))
                reads.append(llm.call(PURPOSE[kind], model, prompt, file_id, SCHEMA, variant=v,
                                      max_tokens=MAX_TOKENS))
        return reads

    reader.model = model
    return reader
