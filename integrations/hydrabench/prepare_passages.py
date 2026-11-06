"""Create a reproducible passage corpus from the supplied KILT JSONL articles."""

import argparse
import hashlib
import json
import re
from pathlib import Path


def prepare(source: Path, output: Path):
    if output.exists() or output.with_suffix(".manifest.json").exists():
        raise FileExistsError("Use a fresh corpus output; frozen passages must not be overwritten.")
    articles = {row["wikipedia_title"]: row for line in source.read_text().splitlines()
                if line and (row := json.loads(line))}
    passages = []
    for title in sorted(articles):
        article = articles[title]
        section = "Abstract"
        paragraphs = []
        sections = []
        for paragraph in article["text"][1:]:
            if paragraph.startswith("Section::::"):
                sections.append((section, paragraphs))
                section = paragraph.removeprefix("Section::::").strip()
                paragraphs = []
            elif paragraph.strip():
                paragraphs.append(paragraph.strip())
        sections.append((section, paragraphs))
        index = 0
        for section, paragraphs in sections:
            text = "\n".join(paragraphs)
            if not text:
                continue
            if section == "Abstract":
                chunks = [text]
            else:
                # The paper does not publish its sentence splitter. This deliberately
                # simple smoke implementation is not claimed to reproduce its index.
                sentences = re.split(r"(?<=[.!?])\s+", text)
                chunks = [" ".join(sentences[i:i + 4]) for i in range(0, len(sentences), 4)]
            for chunk in chunks:
                provenance = article.get("source_url") or article["history"]["url"]
                prefix = f"{title} — {section}\n{provenance}\nWikipedia ID: {article['wikipedia_id']}\n"
                content = prefix + chunk
                passages.append({"uri": f"ctx://team/engineering/hydra/kilt/{article['wikipedia_id']}/{index:04}",
                                 "title": title, "section": section,
                                 "wikipedia_id": article["wikipedia_id"],
                                 "source_url": provenance, "content": content,
                                 "truncated": False})
                index += 1
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in passages))
    manifest = {"original_source": "https://dl.fbaipublicfiles.com/KILT/kilt_knowledgesource.json",
                "retrieved_from": sorted({r.get("source_url") or r["history"]["url"] for r in articles.values()}),
                "snapshot": "2019-08-01", "selection": "All articles in the frozen input JSONL",
                "titles": sorted(articles), "articles": len(articles),
                "passages": len(passages), "character_cap": None,
                "truncated_passages": sum(row["truncated"] for row in passages),
                "source_subset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "passages_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "chunking": "Abstract retained as one passage; body split by punctuation into groups of four sentences; full source text retained without a character cap. Use the document ingestion path separately for article-level navigation."}
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.output)
