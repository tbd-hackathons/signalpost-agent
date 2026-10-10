"""Optional local language model that reads unlabelled careers and news pages.

Many small Norwegian company sites list vacancies or news as plain text, without schema.org markup or
links to a recruiting system, so rule-based extraction finds nothing. A small open-weight model
(Qwen2.5-1.5B-Instruct, Apache-2.0, 4-bit GGUF, CPU via llama.cpp) reads the page text and returns
JSON constrained by a schema.

The model never decides identity (the website gate already did) and its output is not trusted:
every title must occur verbatim in the page text, and every date must occur on the page, or the item
is dropped. Enable with `--llm-model PATH`; without it the agent behaves exactly as before.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct-GGUF:qwen2.5-1.5b-instruct-q4_k_m.gguf"
MAX_PAGE_CHARS = 3500

JOBS_SCHEMA = {"type": "object", "properties": {"jobs": {"type": "array", "maxItems": 15, "items": {
    "type": "object", "properties": {"title": {"type": "string"}, "location": {"type": "string"},
                                     "deadline": {"type": "string"}}, "required": ["title"]}}}, "required": ["jobs"]}
POSTS_SCHEMA = {"type": "object", "properties": {"posts": {"type": "array", "maxItems": 10, "items": {
    "type": "object", "properties": {"title": {"type": "string"}, "date": {"type": "string"}},
    "required": ["title", "date"]}}}, "required": ["posts"]}

JOBS_PROMPT = """You read the text of a Norwegian company's careers page.
List every position the page says is open now (ledig stilling, vi søker, open position).
Copy each job title exactly as written on the page. Add location and application deadline only if written.
Do not list general text such as "send an open application" or "we have no vacancies".
If no position is open, return {"jobs": []}."""
TITLE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}}, "required": ["answer"]}
TITLE_PROMPT = """Answer yes only if the text is the title of one specific job position that a person is hired into,
for example "Elektriker", "Pedagogisk leder", "Platform Engineer".
Answer no for job categories, departments, benefits, student programmes, open applications, or anything else."""
POSTS_PROMPT = """You read the text of a Norwegian company's news or blog page.
List the news posts on the page with their publication date.
Copy each headline exactly as written and copy the date exactly as written. Skip items without a date.
If there are none, return {"posts": []}."""

DATE_PATTERNS = (
    r"\d{4}-\d{2}-\d{2}",
    r"\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}",
    r"\d{1,2}\.?\s+(januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember|"
    r"january|february|march|may|june|july|october|december)\w*\s+\d{4}",
)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.findall(r"\w+", text))


def verbatim(value: str, page: str) -> bool:
    value = fold(value)
    return len(value) >= 4 and value in fold(page)


VACANCY_CUES = ("vi søker", "søker etter", "søker nå", "ledig stilling", "ledige stillinger", "stillingen", "søknadsfrist",
                "søk her", "søk stillingen", "we are hiring", "we're hiring", "is hiring", "vacancy", "apply", "tiltredelse",
                "fast stilling", "vikariat", "heltid", "deltid", "nyopprettet")


def page_blocks(html: str) -> tuple[str, list[str]]:
    """Visible text with one line per block, and the short standalone blocks (headings, list items, links)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        node.decompose()
    lines, standalone = [], []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "li", "a", "p", "td", "strong", "b", "dt", "dd", "div", "span"]):
        if node.find(["p", "li", "div", "h1", "h2", "h3", "h4", "td"]):
            continue  # keep leaf-like blocks only
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        lines.append(text)
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6", "li", "a", "strong", "b", "dt", "td"} and len(text.split()) <= 12:
            standalone.append(text)
    return "\n".join(dict.fromkeys(lines)), standalone


def job_in_context(title: str, page_text: str, standalone: list[str]) -> bool:
    """A title is an advertised job only as its own heading/list item/link, or in a sentence with a hiring cue."""
    t = fold(title)
    if any(t == fold(block) or (t in fold(block) and len(fold(block)) <= len(t) + 25) for block in standalone):
        return True
    for sentence in re.split(r"(?<=[.!?\n])\s+", page_text):
        if t in fold(sentence) and any(cue in sentence.casefold() for cue in VACANCY_CUES):
            return True
    return False


def looks_like_date(value: str) -> bool:
    return any(re.search(p, value or "", re.I) for p in DATE_PATTERNS)


class LocalExtractor:
    """One llama.cpp model shared by all worker threads (calls are serialised)."""

    def __init__(self, model_path: str | Path, *, threads: int = 4, ctx: int = 4096) -> None:
        from llama_cpp import Llama  # optional dependency: `uv sync --extra llm`

        self.model_path = str(model_path)
        self.model_id = MODEL_ID if Path(model_path).name in MODEL_ID else Path(model_path).name
        self.llm = Llama(model_path=self.model_path, n_ctx=ctx, n_threads=threads, verbose=False, seed=0)
        self.lock = threading.Lock()
        self.calls = 0

    def _ask(self, system: str, page: str, schema: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.calls += 1
            out = self.llm.create_chat_completion(
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": page[:MAX_PAGE_CHARS]}],
                response_format={"type": "json_object", "schema": schema},
                temperature=0.0, max_tokens=700)
        try:
            return json.loads(out["choices"][0]["message"]["content"])
        except (json.JSONDecodeError, KeyError, IndexError):
            return {}

    def jobs(self, page_text: str, standalone: list[str] | None = None) -> list[dict[str, Any]]:
        found = []
        for item in (self._ask(JOBS_PROMPT, page_text, JOBS_SCHEMA).get("jobs") or []):
            title = str(item.get("title") or "").strip()
            if not verbatim(title, page_text) or not job_in_context(title, page_text, standalone or []):
                continue  # not on the page, or not presented as a vacancy: dropped
            job = {"title": title}
            for key in ("location", "deadline"):
                value = str(item.get(key) or "").strip()
                if value and verbatim(value, page_text):
                    job[key] = value
            found.append(job)
        jobs = list({fold(j["title"]): j for j in found}.values())
        return [j for j in jobs if self.is_job_title(j["title"])]

    def is_job_title(self, title: str) -> bool:
        return self._ask(TITLE_PROMPT, title, TITLE_SCHEMA).get("answer") == "yes"

    def posts(self, page_text: str) -> list[dict[str, Any]]:
        found = []
        for item in (self._ask(POSTS_PROMPT, page_text, POSTS_SCHEMA).get("posts") or []):
            title, date = str(item.get("title") or "").strip(), str(item.get("date") or "").strip()
            if verbatim(title, page_text) and looks_like_date(date) and verbatim(date, page_text):
                found.append({"title": title, "date": date})
        return list({fold(p["title"]): p for p in found}.values())
