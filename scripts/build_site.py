#!/usr/bin/env python3
from __future__ import annotations

import html
import re
import shutil
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "site"
DOWNLOADS = OUTPUT / "downloads"
PREVIEWS = OUTPUT / "preview"

SOURCE_GROUPS = ["2014", "2018", "arquivos"]
SOURCE_EXTENSIONS = {".pdf", ".odt"}

TITLE_OVERRIDES = {
    "2014/Manual_Instalacao_e-Cidade_Ubuntu-12.04-Server-LTS.odt": "Manual de Instalação do e-Cidade - Ubuntu 12.04 Server LTS",
    "2014/Manual_Instalacao_e-Cidade-Transparencia_Ubuntu-12.04-Server-LTS.odt": "Manual de Instalação do e-Cidade - Transparência",
    "2018/Manual_e-cidade_ubuntu20.04.pdf": "Manual e-cidade - Ubuntu 20.04",
    "arquivos/Apostila-encerramento-e-abertura-calendarios-letivos.pdf": "Apostila: encerramento e abertura de calendários letivos",
    "arquivos/Apostila_DBEducação_1a._etapa_Capacitação.pdf": "Apostila DB Educação - 1ª etapa de capacitação",
    "arquivos/Apostila_DBEducação_2a._etapa_Capacitação.pdf": "Apostila DB Educação - 2ª etapa de capacitação",
    "arquivos/CAPACITAÇÃO-PROFESSORES.pdf": "Capacitação de professores",
    "arquivos/Manual_Material.pdf": "Manual de Material",
    "arquivos/Manual_Patrimonio.pdf": "Manual de Patrimônio",
    "arquivos/Manual_Protocolo.pdf": "Manual de Protocolo",
    "arquivos/Manual_Sistema_de_Atendimento_Módulo_Cliente.pdf": "Manual Sistema de Atendimento - Módulo Cliente",
    "arquivos/Passo-a-passo-para-cadastro-de-usuários-Bairros-Logradouros.pdf": "Passo a passo para cadastro de usuários - Bairros e logradouros",
}


@dataclass(frozen=True)
class Document:
    source: Path
    group: str
    title: str
    slug: str
    preview: Path
    download: Path

    @property
    def rel_source(self) -> str:
        return f"{self.group}/{self.source.name}"

    @property
    def ext(self) -> str:
        return self.source.suffix.lower().lstrip(".")


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "documento"


def prettify_title(stem: str) -> str:
    text = stem.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Documento"
    parts = []
    for part in text.split(" "):
        if part.isupper() or any(ch.isdigit() for ch in part):
            parts.append(part)
        elif part.lower() in {"e", "de", "do", "da", "dos", "das", "em", "a", "o", "os", "as"}:
            parts.append(part.lower())
        else:
            parts.append(part[:1].upper() + part[1:])
    return " ".join(parts)


def discover_documents() -> list[Document]:
    documents: list[Document] = []
    for group in SOURCE_GROUPS:
        group_dir = ROOT / group
        if not group_dir.exists():
            continue
        for source in sorted(group_dir.iterdir()):
            if not source.is_file() or source.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            rel = f"{group}/{source.name}"
            title = TITLE_OVERRIDES.get(rel, prettify_title(source.stem))
            slug = slugify(source.stem)
            preview = PREVIEWS / group / slug / "index.html"
            download = DOWNLOADS / group / source.name
            documents.append(Document(source, group, title, slug, preview, download))
    return documents


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_static_assets() -> None:
    (OUTPUT / ".nojekyll").write_text("", encoding="utf-8")


def copy_original(doc: Document) -> None:
    target = doc.download
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(doc.source, target)


@dataclass(frozen=True)
class StyleFlags:
    bold: bool = False
    italic: bool = False
    underline: bool = False


def _attr_value(attrs: dict[str, str], suffix: str) -> str | None:
    for key, value in attrs.items():
        if key.rsplit("}", 1)[-1] == suffix:
            return value
    return None


def _style_flags_from_props(attrs: dict[str, str]) -> StyleFlags:
    weight = _attr_value(attrs, "font-weight")
    font_style = _attr_value(attrs, "font-style")
    underline = _attr_value(attrs, "text-underline-style")
    return StyleFlags(
        bold=weight == "bold",
        italic=font_style == "italic",
        underline=bool(underline and underline != "none"),
    )


def _merge_flags(parent: StyleFlags, child: StyleFlags) -> StyleFlags:
    return StyleFlags(
        bold=parent.bold or child.bold,
        italic=parent.italic or child.italic,
        underline=parent.underline or child.underline,
    )


def collect_style_flags(*roots: ET.Element) -> dict[str, StyleFlags]:
    ns = {"style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0"}
    raw: dict[str, tuple[str | None, StyleFlags]] = {}
    for root in roots:
        for style in root.findall(".//style:style", ns) + root.findall(".//style:default-style", ns):
            name = style.attrib.get(f'{{{ns["style"]}}}name')
            if not name:
                continue
            parent = style.attrib.get(f'{{{ns["style"]}}}parent-style-name')
            props = style.find("style:text-properties", ns)
            flags = _style_flags_from_props(props.attrib if props is not None else {})
            raw[name] = (parent, flags)

    resolved: dict[str, StyleFlags] = {}

    def resolve(name: str, trail: set[str] | None = None) -> StyleFlags:
        if name in resolved:
            return resolved[name]
        trail = set() if trail is None else trail
        if name in trail:
            return StyleFlags()
        trail.add(name)
        parent, flags = raw.get(name, (None, StyleFlags()))
        if parent and parent in raw:
            flags = _merge_flags(resolve(parent, trail), flags)
        resolved[name] = flags
        return flags

    for name in list(raw):
        resolve(name)
    return resolved


def redact_dbseller(html_text: str) -> str:
    replacements = [
        (r"dbseller\.com\.br", "e-cidade.example"),
        (r"DBSeller\s+Sistemas\s+Integrados", "e-Cidade"),
        (r"DBSeller\s+Serviços\s+de\s+Informática\s+LTDA\.?", "e-Cidade"),
        (r"\bDBSeller\b", "e-Cidade"),
        (r"\bdbseller\b", "admin"),
    ]
    for pattern, replacement in replacements:
        html_text = re.sub(pattern, replacement, html_text, flags=re.IGNORECASE)
    return html_text


def preview_image_for(doc: Document) -> Path | None:
    candidates = sorted(doc.preview.parent.glob("*.png"))
    if not candidates:
        return None
    return candidates[0]


def feature_documents(documents: list[Document], limit: int = 3) -> list[Document]:
    featured: list[Document] = []
    preferred_groups = ["2018", "arquivos", "2014"]
    for group in preferred_groups:
        for doc in documents:
            if doc.group != group or doc.ext != "pdf":
                continue
            if preview_image_for(doc) is None:
                continue
            featured.append(doc)
            if len(featured) >= limit:
                return featured
    if len(featured) < limit:
        for doc in documents:
            if doc.ext != "pdf" or preview_image_for(doc) is None or doc in featured:
                continue
            featured.append(doc)
            if len(featured) >= limit:
                break
    return featured


def convert_pdf(doc: Document) -> None:
    output_dir = doc.preview.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / "source"
    command = [
        "pdftohtml",
        "-s",
        "-noframes",
        str(doc.source),
        str(base),
    ]
    subprocess.run(command, check=True, cwd=output_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    generated = base.with_suffix(".html")
    generated.rename(doc.preview)
    html_text = doc.preview.read_text(encoding="utf-8", errors="ignore")
    html_text = re.sub(r"(<title>).*?(</title>)", rf"\1{html.escape(doc.title)}\2", html_text, count=1, flags=re.S)
    html_text = html_text.replace(
        '<body bgcolor="#A0A0A0" vlink="blue" link="blue">',
        '<body class="pdf-body">',
        1,
    )
    html_text = html_text.replace(
        "</head>",
        """
    <style>
      :root {
        color-scheme: light;
        --bg: #eef8f2;
        --panel: rgba(255, 255, 255, 0.9);
        --text: #173126;
        --muted: #557063;
        --accent: #1f7745;
        --accent-2: #23b0dd;
        --accent-3: #ffcb1f;
        --border: rgba(31, 119, 69, 0.14);
      }
      body.pdf-body {
        margin: 0;
        padding: 24px;
        font-family: Georgia, "Times New Roman", Times, serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(255, 203, 31, 0.16), transparent 28%),
          radial-gradient(circle at 85% 15%, rgba(35, 176, 221, 0.16), transparent 28%),
          linear-gradient(180deg, var(--bg) 0%, #f8fbfd 48%, #edf7ef 100%);
      }
      .pdf-shell {
        max-width: 1180px;
        margin: 0 auto;
      }
      .pdf-topbar {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: center;
        margin-bottom: 18px;
        padding: 18px 22px;
        border: 1px solid var(--border);
        border-radius: 20px;
        background: var(--panel);
        box-shadow: 0 22px 70px rgba(52, 38, 20, 0.12);
        backdrop-filter: blur(18px);
      }
      .pdf-topbar h1 {
        margin: 0;
        font-size: 1.2rem;
        line-height: 1.2;
        color: var(--text);
      }
      .pdf-topbar p {
        margin: 4px 0 0;
        color: var(--muted);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.92rem;
      }
      .pdf-topbar a {
        color: var(--accent);
        text-decoration: none;
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-weight: 700;
      }
      .pdf-pages {
        display: grid;
        gap: 22px;
      }
      .pdf-pages > div[id^="page"] {
        margin: 0 auto;
        overflow: hidden;
        border-radius: 18px;
        border: 1px solid var(--border);
        box-shadow: 0 18px 48px rgba(31, 119, 69, 0.12);
        background: #fff;
      }
      .pdf-pages > div[id^="page"] img {
        display: block;
        max-width: 100%;
        height: auto;
      }
      .pdf-pages > div[id^="page"] > p {
        line-height: 1.45;
      }
      @media (max-width: 760px) {
        body.pdf-body {
          padding: 12px;
        }
        .pdf-topbar {
          flex-direction: column;
          align-items: flex-start;
          padding: 16px 18px;
        }
      }
    </style>
    </head>""",
        1,
    )
    html_text = html_text.replace(
        "<body class=\"pdf-body\">",
        f"""<body class="pdf-body">
    <div class="pdf-shell">
      <header class="pdf-topbar">
        <div>
          <h1>{html.escape(doc.title)}</h1>
          <p>Visualização gerada automaticamente a partir do PDF original.</p>
        </div>
        <div>
          <a href="{quote(doc.download.relative_to(OUTPUT).as_posix(), safe='/')}">Baixar original</a>
        </div>
      </header>
      <main class="pdf-pages">""",
        1,
    )
    html_text = html_text.replace("</body>", "      </main>\n    </div>\n  </body>", 1)
    html_text = redact_dbseller(html_text)
    doc.preview.write_text(html_text, encoding="utf-8")


def _odt_text(node: ET.Element, ns: dict[str, str], styles: dict[str, StyleFlags]) -> str:
    parts: list[str] = []
    if node.text:
        parts.append(html.escape(node.text))
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in {"span", "a", "s", "line-break", "tab"}:
            parts.append(_render_odt_inline(child, ns, styles))
        elif tag in {"p", "h"}:
            parts.append(_render_odt_paragraph(child, ns, styles))
        elif tag in {"list", "table", "section"}:
            parts.append(_render_odt_element(child, ns, styles))
        else:
            parts.append(_odt_text(child, ns, styles))
        if child.tail:
            parts.append(html.escape(child.tail))
    return "".join(parts)


def _render_odt_inline(node: ET.Element, ns: dict[str, str], styles: dict[str, StyleFlags]) -> str:
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "s":
        count = node.attrib.get(f'{{{ns["text"]}}}c', "1")
        return " " * max(1, int(count))
    if tag == "line-break":
        return "<br/>"
    if tag == "tab":
        return "&emsp;"
    if tag == "span":
        content = _odt_text(node, ns, styles)
        flags = styles.get(node.attrib.get(f'{{{ns["text"]}}}style-name', ""), StyleFlags())
        if flags.bold:
            content = f"<strong>{content}</strong>"
        if flags.italic:
            content = f"<em>{content}</em>"
        if flags.underline:
            content = f"<u>{content}</u>"
        return content
    if tag == "a":
        href = node.attrib.get(f'{{{ns["xlink"]}}}href') or node.attrib.get("href") or "#"
        return f'<a href="{html.escape(href, quote=True)}">{_odt_text(node, ns, styles)}</a>'
    return _odt_text(node, ns, styles)


def _render_odt_paragraph(
    node: ET.Element,
    ns: dict[str, str],
    styles: dict[str, StyleFlags],
    level: int | None = None,
) -> str:
    content = _odt_text(node, ns, styles)
    if not content.strip():
        return ""
    if level is not None:
        return f"<h{level}>{content}</h{level}>"
    style_name = node.attrib.get(f'{{{ns["text"]}}}style-name', "")
    flags = styles.get(style_name, StyleFlags())
    if flags.bold and not content.startswith("<strong>"):
        content = f"<strong>{content}</strong>"
    if flags.italic and not content.startswith("<em>"):
        content = f"<em>{content}</em>"
    if flags.underline and not content.startswith("<u>"):
        content = f"<u>{content}</u>"
    if style_name and style_name.lower().startswith("p") and style_name[1:].isdigit():
        size = int(style_name[1:])
        if size <= 3:
            return f"<h2>{content}</h2>"
        if size <= 7:
            return f"<h3>{content}</h3>"
    return f"<p>{content}</p>"


def _render_odt_table(node: ET.Element, ns: dict[str, str], styles: dict[str, StyleFlags]) -> str:
    rows: list[str] = []
    for row in node:
        if row.tag.rsplit("}", 1)[-1] != "table-row":
            continue
        cells: list[str] = []
        for cell in row:
            if cell.tag.rsplit("}", 1)[-1] != "table-cell":
                continue
            cell_blocks: list[str] = []
            for item in cell:
                tag = item.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    rendered = _render_odt_paragraph(item, ns, styles)
                    if rendered:
                        cell_blocks.append(rendered)
                elif tag == "list":
                    cell_blocks.append(_render_odt_list(item, ns, styles))
                else:
                    nested = _render_odt_element(item, ns, styles)
                    if nested:
                        cell_blocks.append(nested)
            cells.append(f"<td>{''.join(cell_blocks)}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table>{''.join(rows)}</table>"


def _render_odt_list(node: ET.Element, ns: dict[str, str], styles: dict[str, StyleFlags]) -> str:
    items: list[str] = []
    ordered = "continue-numbering" in node.attrib or node.attrib.get(f'{{{ns["text"]}}}style-name', "").upper().startswith("N")
    for item in node:
        if item.tag.rsplit("}", 1)[-1] != "list-item":
            continue
        bits: list[str] = []
        for item_child in item:
            tag = item_child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                rendered = _render_odt_paragraph(item_child, ns, styles)
                if rendered:
                    bits.append(rendered)
            elif tag == "list":
                bits.append(_render_odt_list(item_child, ns, styles))
            elif tag == "table":
                bits.append(_render_odt_table(item_child, ns, styles))
            else:
                nested = _render_odt_element(item_child, ns, styles)
                if nested:
                    bits.append(nested)
            if item_child.tail:
                bits.append(html.escape(item_child.tail))
        items.append(f"<li>{''.join(bits)}</li>")
    tag_name = "ol" if ordered else "ul"
    return f"<{tag_name}>{''.join(items)}</{tag_name}>"


def _render_odt_element(node: ET.Element, ns: dict[str, str], styles: dict[str, StyleFlags]) -> str:
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "h":
        level = int(node.attrib.get(f'{{{ns["text"]}}}outline-level', "1"))
        level = min(max(level, 1), 6)
        return _render_odt_paragraph(node, ns, styles, level=level)
    if tag == "p":
        return _render_odt_paragraph(node, ns, styles)
    if tag == "list":
        return _render_odt_list(node, ns, styles)
    if tag == "table":
        return _render_odt_table(node, ns, styles)
    if tag == "section":
        return _render_odt_blocks(node, ns, styles)
    if tag == "a":
        return _render_odt_inline(node, ns, styles)
    return _odt_text(node, ns, styles)


def _render_odt_blocks(node: ET.Element, ns: dict[str, str], styles: dict[str, StyleFlags]) -> str:
    blocks: list[str] = []
    for child in node:
        rendered = _render_odt_element(child, ns, styles)
        if rendered:
            blocks.append(rendered)
        if child.tail:
            tail = html.escape(child.tail)
            if tail.strip():
                blocks.append(f"<p>{tail}</p>")
    return "".join(blocks)


def convert_odt(doc: Document) -> None:
    output_dir = doc.preview.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(doc.source) as archive:
        content = archive.read("content.xml")
        styles_xml = archive.read("styles.xml") if "styles.xml" in archive.namelist() else None
    ns = {
        "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
        "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
        "xlink": "http://www.w3.org/1999/xlink",
    }
    root = ET.fromstring(content)
    styles_root = ET.fromstring(styles_xml) if styles_xml is not None else ET.Element("styles")
    style_flags = collect_style_flags(root, styles_root)
    body = root.find("office:body/office:text", ns)
    if body is None:
        raise RuntimeError(f"ODT body not found: {doc.source}")
    body_html = _render_odt_blocks(body, ns, style_flags)
    body_html = redact_dbseller(body_html)
    page = f"""<!doctype html>
<html lang="pt-br">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(doc.title)}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #eef8f2;
        --panel: rgba(255, 255, 255, 0.9);
        --text: #173126;
        --muted: #557063;
        --accent: #1f7745;
        --accent-2: #23b0dd;
        --accent-3: #ffcb1f;
        --border: rgba(31, 119, 69, 0.14);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        padding: 24px;
        font-family: Georgia, "Times New Roman", Times, serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(255, 203, 31, 0.16), transparent 28%),
          radial-gradient(circle at 85% 15%, rgba(35, 176, 221, 0.16), transparent 28%),
          linear-gradient(180deg, var(--bg) 0%, #f8fbfd 48%, #edf7ef 100%);
      }}
      .page {{
        max-width: 960px;
        margin: 0 auto;
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 20px 60px rgba(31, 119, 69, 0.12);
        overflow: hidden;
      }}
      .topbar {{
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: center;
        padding: 18px 22px;
        border-bottom: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.78);
      }}
      .topbar h1 {{
        margin: 0;
        font-size: 1.3rem;
      }}
      .topbar p {{
        margin: 4px 0 0;
        color: var(--muted);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.92rem;
      }}
      .topbar a {{
        color: var(--accent);
        text-decoration: none;
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-weight: 700;
      }}
      .content {{
        padding: 24px 22px 28px;
        line-height: 1.75;
        font-size: 1rem;
      }}
      h1, h2, h3, h4, h5, h6 {{
        line-height: 1.2;
      }}
      p, ul {{
        margin: 0 0 1rem;
      }}
      ul {{
        padding-left: 1.4rem;
      }}
      a {{
        color: var(--accent);
      }}
      img {{
        max-width: 100%;
        height: auto;
      }}
    </style>
  </head>
  <body>
    <main class="page">
      <header class="topbar">
        <div>
          <h1>{html.escape(doc.title)}</h1>
          <p>Conversão automática a partir de ODT para publicação estática.</p>
        </div>
        <div>
          <a href="{quote(doc.download.relative_to(OUTPUT).as_posix(), safe='/')}">Baixar original</a>
        </div>
      </header>
      <article class="content">
        {body_html}
      </article>
    </main>
  </body>
</html>
"""
    doc.preview.write_text(page, encoding="utf-8")


def render_index(documents: Iterable[Document]) -> str:
    documents = list(documents)
    by_group: dict[str, list[Document]] = {}
    for doc in documents:
        by_group.setdefault(doc.group, []).append(doc)
    for group_docs in by_group.values():
        group_docs.sort(key=lambda item: item.title.lower())

    featured_docs = feature_documents(documents, limit=3)
    featured_cards: list[str] = []
    for index, doc in enumerate(featured_docs):
        image = preview_image_for(doc)
        if image is None:
            continue
        featured_cards.append(
            f"""
          <a class="hero-frame {'hero-frame-large' if index == 0 else 'hero-frame-mini'}" href="{quote(doc.preview.relative_to(OUTPUT).as_posix(), safe='/')}">
            <img src="{quote(image.relative_to(OUTPUT).as_posix(), safe='/')}" alt="{html.escape(doc.title)}" />
            <div class="hero-caption">
              <span>{html.escape(doc.group)}</span>
              <strong>{html.escape(doc.title)}</strong>
            </div>
          </a>
            """.strip()
        )
    if len(featured_cards) == 1:
        featured_cards.append('<div class="hero-frame hero-frame-mini hero-frame-placeholder"></div>')
        featured_cards.append('<div class="hero-frame hero-frame-mini hero-frame-placeholder alt"></div>')
    elif len(featured_cards) == 2:
        featured_cards.append('<div class="hero-frame hero-frame-mini hero-frame-placeholder"></div>')

    group_blocks: list[str] = []
    for group in SOURCE_GROUPS:
        group_docs = by_group.get(group, [])
        if not group_docs:
            continue
        group_blocks.append(
            """
        <section class="group">
          <div class="group-head">
            <div>
              <p class="group-kicker">{group}</p>
              <h2>{heading}</h2>
            </div>
            <p class="group-note">{note}</p>
          </div>
          <ul class="doc-list">
            {items}
          </ul>
        </section>
            """.strip().format(
                group=html.escape(group),
                heading=html.escape({
                    "2014": "Instalação e transparência",
                    "2018": "Atualização para Ubuntu 20.04",
                    "arquivos": "Manuais recebidos em lote",
                }.get(group, group)),
                note=html.escape({
                    "2014": "Documentos originais em OpenDocument Text.",
                    "2018": "Manual em PDF para leitura rápida.",
                    "arquivos": "Todos os arquivos foram incorporados ao catálogo e terão preview em HTML.",
                }.get(group, "")),
                items="\n            ".join(
                    f"""
              <li class="doc-item">
                <a class="doc-link" href="{quote(doc.preview.relative_to(OUTPUT).as_posix(), safe='/')}">
                  <span class="doc-title">{html.escape(doc.title)}</span>
                  <span class="doc-meta">HTML</span>
                </a>
                <a class="download-link" href="{quote(doc.download.relative_to(OUTPUT).as_posix(), safe='/')}">
                  Baixar original {html.escape(doc.ext.upper())}
                </a>
              </li>
                    """.strip()
                    for doc in group_docs
                ),
            )
        )

    total = len(documents)
    return f"""<!doctype html>
<html lang="pt-br">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="description" content="Catálogo estático de manuais do e-Cidade e materiais relacionados." />
    <title>Manuais do e-Cidade</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #eef8f2;
        --bg-accent: #edf7ef;
        --panel: rgba(255, 255, 255, 0.9);
        --border: rgba(31, 119, 69, 0.14);
        --text: #173126;
        --muted: #557063;
        --strong: #123126;
        --accent: #1f7745;
        --accent-2: #23b0dd;
        --accent-3: #ffcb1f;
        --accent-soft: rgba(31, 119, 69, 0.12);
        --accent-warm: rgba(255, 203, 31, 0.2);
        --shadow: 0 22px 70px rgba(31, 119, 69, 0.12);
        --radius-xl: 28px;
        --radius-lg: 20px;
        --radius-md: 14px;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        color: var(--text);
        font-family: Georgia, "Times New Roman", Times, serif;
        background:
          radial-gradient(circle at top left, rgba(255, 203, 31, 0.16), transparent 28%),
          radial-gradient(circle at 85% 15%, rgba(35, 176, 221, 0.16), transparent 28%),
          linear-gradient(180deg, var(--bg) 0%, #f8fbfd 42%, var(--bg-accent) 100%);
      }}
      a {{ color: inherit; text-decoration: none; }}
      .page-shell {{
        width: min(1120px, calc(100% - 32px));
        margin: 0 auto;
        padding: 32px 0 56px;
      }}
      .hero, .group, .footer {{
        background: var(--panel);
        border: 1px solid var(--border);
        box-shadow: var(--shadow);
        backdrop-filter: blur(18px);
      }}
      .hero {{
        display: grid;
        grid-template-columns: minmax(0, 1.1fr) minmax(320px, 0.9fr);
        gap: 24px;
        align-items: center;
        border-radius: var(--radius-xl);
        padding: 34px 32px 30px;
      }}
      .eyebrow, .group-kicker {{
        margin: 0 0 10px;
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.16em;
        font-size: 0.78rem;
        font-weight: 700;
      }}
      h1 {{
        margin: 0;
        max-width: 11ch;
        color: var(--strong);
        font-size: clamp(3rem, 7vw, 5.4rem);
        line-height: 0.94;
        letter-spacing: -0.05em;
      }}
      .lead {{
        margin: 18px 0 0;
        max-width: 62ch;
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 1.06rem;
        line-height: 1.6;
        color: var(--muted);
      }}
      .stats {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 16px;
        margin-top: 26px;
      }}
      .stat {{
        border-radius: var(--radius-md);
        padding: 18px 18px 16px;
        background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(241, 248, 244, 0.96));
        border: 1px solid rgba(31, 119, 69, 0.14);
      }}
      .hero-visual {{
        display: grid;
        grid-template-columns: 1.15fr 0.85fr;
        gap: 14px;
        min-height: 420px;
      }}
      .hero-stack {{
        display: grid;
        gap: 14px;
      }}
      .hero-frame {{
        position: relative;
        display: block;
        overflow: hidden;
        border-radius: 24px;
        border: 1px solid rgba(31, 119, 69, 0.16);
        background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(238, 248, 242, 0.92));
        box-shadow: 0 20px 48px rgba(31, 119, 69, 0.14);
      }}
      .hero-frame-large {{
        min-height: 420px;
      }}
      .hero-frame-mini {{
        min-height: 203px;
      }}
      .hero-frame img {{
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
      }}
      .hero-caption {{
        position: absolute;
        inset: auto 0 0 0;
        padding: 18px 18px 16px;
        color: #fff;
        background: linear-gradient(180deg, transparent 0%, rgba(18, 49, 38, 0.88) 100%);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      }}
      .hero-caption span {{
        display: inline-flex;
        margin-bottom: 6px;
        padding: 5px 8px;
        border-radius: 999px;
        background: rgba(255,255,255,0.15);
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }}
      .hero-caption strong {{
        display: block;
        font-size: 1rem;
        line-height: 1.35;
      }}
      .hero-frame-placeholder {{
        background:
          radial-gradient(circle at 28% 25%, rgba(31, 119, 69, 0.18), transparent 0 30%),
          radial-gradient(circle at 72% 70%, rgba(35, 176, 221, 0.18), transparent 0 30%),
          linear-gradient(160deg, rgba(255,255,255,0.92), rgba(237, 247, 239, 0.92));
      }}
      .hero-frame-placeholder.alt {{
        background:
          radial-gradient(circle at 72% 26%, rgba(255, 203, 31, 0.22), transparent 0 30%),
          radial-gradient(circle at 24% 72%, rgba(35, 176, 221, 0.18), transparent 0 30%),
          linear-gradient(160deg, rgba(255,255,255,0.92), rgba(235, 244, 248, 0.92));
      }}
      .stat-value {{
        display: block;
        color: var(--strong);
        font-size: 1.5rem;
        font-weight: 700;
        line-height: 1;
      }}
      .stat-label {{
        display: block;
        margin-top: 6px;
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        color: var(--muted);
        font-size: 0.95rem;
      }}
      .catalog {{
        display: grid;
        gap: 20px;
        margin-top: 22px;
      }}
      .group {{
        border-radius: var(--radius-xl);
        padding: 24px;
      }}
      .group-head {{
        display: flex;
        gap: 16px;
        justify-content: space-between;
        align-items: start;
        margin-bottom: 18px;
      }}
      .group-head h2 {{
        margin: 0;
        color: var(--strong);
        font-size: clamp(1.45rem, 3vw, 2rem);
        line-height: 1.1;
      }}
      .group-note {{
        margin: 0;
        max-width: 34ch;
        color: var(--muted);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.96rem;
        line-height: 1.5;
        text-align: right;
      }}
      .doc-list {{
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: 10px;
      }}
      .doc-item {{
        margin: 0;
        display: grid;
        gap: 8px;
      }}
      .doc-link {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        padding: 16px 18px;
        border-radius: 16px;
        background: rgba(255, 255, 255, 0.76);
        border: 1px solid rgba(36, 32, 28, 0.08);
      }}
      .doc-link:hover {{
        border-color: rgba(86, 115, 58, 0.28);
        background: rgba(255, 255, 255, 0.94);
      }}
      .doc-title {{
        color: var(--strong);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.98rem;
        line-height: 1.45;
      }}
      .doc-meta {{
        flex: 0 0 auto;
        border-radius: 999px;
        padding: 7px 10px;
        background: var(--accent-soft);
        color: var(--accent);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      .download-link {{
        display: inline-flex;
        width: fit-content;
        padding: 0 18px 4px;
        color: var(--accent);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.9rem;
        font-weight: 700;
      }}
      .download-link:hover {{
        text-decoration: underline;
      }}
      .footer {{
        margin-top: 20px;
        border-radius: var(--radius-lg);
        padding: 18px 22px;
      }}
      .footer p {{
        margin: 0;
        color: var(--muted);
        font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
        font-size: 0.95rem;
        line-height: 1.55;
      }}
      @media (max-width: 760px) {{
        .page-shell {{
          width: min(100% - 20px, 1120px);
          padding-top: 12px;
        }}
        .hero, .group {{
          padding: 22px 18px;
        }}
        .hero {{
          grid-template-columns: 1fr;
        }}
        .hero-visual {{
          grid-template-columns: 1fr;
          min-height: auto;
        }}
        .hero-frame-large,
        .hero-frame-mini {{
          min-height: 220px;
        }}
        .stats {{
          grid-template-columns: 1fr;
        }}
        .group-head {{
          flex-direction: column;
        }}
        .group-note {{
          text-align: left;
        }}
        .doc-link {{
          align-items: flex-start;
          flex-direction: column;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="page-shell">
      <header class="hero">
        <div class="hero-copy">
          <div class="eyebrow">Publicação estática</div>
          <h1>Manuais do e-Cidade</h1>
          <p class="lead">Catálogo gerado automaticamente a partir dos arquivos originais do repositório. As miniaturas da capa usam imagens reais dos manuais para dar contexto visual imediato ao conjunto.</p>
          <div class="stats" aria-label="Resumo do catálogo">
            <div class="stat"><span class="stat-value">{total}</span><span class="stat-label">arquivos publicados</span></div>
            <div class="stat"><span class="stat-value">{len(by_group)}</span><span class="stat-label">grupos</span></div>
            <div class="stat"><span class="stat-value">HTML</span><span class="stat-label">pronto para Pages</span></div>
          </div>
        </div>
        <div class="hero-visual" aria-label="Miniaturas dos manuais">
          <div class="hero-stack" style="grid-column: span 1;">
            {"".join(featured_cards[:1])}
          </div>
          <div class="hero-stack" style="grid-column: span 1;">
            {"".join(featured_cards[1:])}
          </div>
        </div>
      </header>
      <main class="catalog">
        {"".join(group_blocks)}
      </main>
      <footer class="footer">
        <p>O build publica a pasta `site/`. Para incluir novos manuais, basta adicionar os arquivos nas pastas de origem e rodar o gerador novamente.</p>
      </footer>
    </div>
  </body>
</html>
"""


def build_site() -> None:
    docs = discover_documents()
    ensure_clean_dir(OUTPUT)
    copy_static_assets()
    for doc in docs:
        copy_original(doc)
        if doc.ext == "pdf":
            convert_pdf(doc)
        elif doc.ext == "odt":
            convert_odt(doc)
        else:
            raise RuntimeError(f"Unsupported document type: {doc.source}")
    (OUTPUT / "index.html").write_text(render_index(docs), encoding="utf-8")


def main() -> None:
    build_site()


if __name__ == "__main__":
    main()
