"""A small Markdown reader for the design doc (EDD) and the conclusion file, which use fixed headings, and a tiny Markdown to HTML converter
for the subset they use: paragraphs, bold, code, links, bullet and numbered lists, tables, and ### headings."""
import html
import re
from dataclasses import dataclass, field


@dataclass
class Doc:
    title: str                                    # the first line without its "# "
    header: dict[str, str] = field(default_factory=dict)      # the "- **Label:** value" lines before the first section
    sections: dict[str, str] = field(default_factory=dict)    # "## Heading" -> its Markdown body (### subsections stay inside)


def parse_doc(text: str) -> Doc:
    """Split a document into its title, its header block and its ## sections."""
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("the document must start with a '# Title' line")
    doc, current = Doc(title=lines[0][2:].strip()), None
    for line in lines[1:]:
        if line.startswith("## "):
            current = line[3:].strip()
            doc.sections[current] = ""
        elif current is None:
            m = re.match(r"^- \*\*(.+?):\*\*\s*(.*)$", line)
            if m:
                doc.header[m.group(1)] = m.group(2).strip()
        else:
            doc.sections[current] += line + "\n"
    doc.sections = {k: v.strip() for k, v in doc.sections.items()}
    return doc


def fields(body: str) -> dict[str, str]:
    """The '**Label:** text' paragraphs of a section body."""
    out = {}
    for block in re.split(r"\n\s*\n", body.strip()):
        m = re.match(r"^\*\*(.+?):\*\*\s*(.*)$", block.strip(), flags=re.S)
        if m:
            out[m.group(1)] = " ".join(m.group(2).split())
    return out


def table(body: str) -> list[dict[str, str]]:
    """The first Markdown table in a body as a list of {column: cell} rows."""
    rows = [l for l in body.splitlines() if l.strip().startswith("|")]
    if len(rows) < 2:
        return []
    cells = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
    head = cells(rows[0])
    return [dict(zip(head, cells(l))) for l in rows[2:]]


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|[\w./#-]+)\)", r'<a href="\2">\1</a>', text)
    return text


def to_html(body: str) -> str:
    """Markdown (the subset above) as HTML. Text is escaped first, so nothing in a document can inject markup."""
    out, lines, i = [], body.strip().splitlines(), 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
        elif line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
            i += 1
        elif line.lstrip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            cells = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
            head, rows = cells(block[0]), [cells(l) for l in block[2:]]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")
        elif re.match(r"^\s*(- |\d+\. )", line):
            ordered = bool(re.match(r"^\s*\d+\. ", line))
            items = []
            while i < len(lines) and re.match(r"^\s*(- |\d+\. )", lines[i]):
                items.append(re.sub(r"^\s*(- |\d+\. )", "", lines[i]).strip())
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith("### ") and not lines[i].lstrip().startswith("|") \
                    and not re.match(r"^\s*(- |\d+\. )", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)
