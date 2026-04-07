#!/usr/bin/env python3
"""Generate a professional PDF from POC_CLIENT_PRESENTATION.md."""
import re
import subprocess
import tempfile
import sys
from pathlib import Path
from markdown import markdown
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent.parent
MD_FILE = ROOT / "POC_CLIENT_PRESENTATION.md"
OUT_PDF = ROOT / "build" / "POC_Client_Presentation.pdf"

CSS = """
@page {
    size: A4;
    margin: 25mm 20mm 25mm 20mm;
    @bottom-center {
        content: "Confidential — Kairos Edge Deployment POC";
        font-size: 8pt;
        color: #999;
    }
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-size: 8pt;
        color: #999;
    }
}
body {
    font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.55;
    color: #1a1a1a;
    max-width: 100%;
    text-align: justify;
    hyphens: auto;
}
h1 {
    font-size: 22pt;
    color: #0d2137;
    border-bottom: 3px solid #2563eb;
    padding-bottom: 8px;
    margin-top: 30px;
    page-break-after: avoid;
}
h2 {
    font-size: 16pt;
    color: #1e3a5f;
    border-bottom: 1.5px solid #cbd5e1;
    padding-bottom: 5px;
    margin-top: 25px;
    page-break-after: avoid;
}
h3 {
    font-size: 13pt;
    color: #334155;
    margin-top: 20px;
    page-break-after: avoid;
    page-break-inside: avoid;
}
h3 + p, h3 + ul, h3 + ol, h3 + table, h3 + blockquote {
    page-break-before: avoid;
}
h4 {
    font-size: 11pt;
    color: #475569;
    margin-top: 15px;
    page-break-after: avoid;
    page-break-inside: avoid;
}
h4 + p, h4 + ul, h4 + ol, h4 + table {
    page-break-before: avoid;
}
p, li, td, th {
    orphans: 3;
    widows: 3;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
}
th {
    background: #1e3a5f;
    color: white;
    padding: 8px 10px;
    text-align: left;
    font-weight: 600;
}
td {
    padding: 6px 10px;
    border-bottom: 1px solid #e2e8f0;
    vertical-align: top;
}
tr:nth-child(even) td {
    background: #f8fafc;
}
code {
    background: #f1f5f9;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 9pt;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}
pre {
    background: #0f172a;
    color: #e2e8f0;
    padding: 14px 16px;
    border-radius: 6px;
    font-size: 8.5pt;
    line-height: 1.45;
    overflow-x: auto;
    page-break-inside: avoid;
    margin: 12px 0;
}
pre code {
    background: none;
    padding: 0;
    color: inherit;
    font-size: inherit;
}
blockquote {
    border-left: 4px solid #2563eb;
    margin: 12px 0;
    padding: 8px 16px;
    background: #eff6ff;
    color: #1e40af;
    page-break-inside: avoid;
}
hr {
    border: none;
    border-top: 2px solid #e2e8f0;
    margin: 25px 0;
}
ul, ol {
    page-break-inside: avoid;
}
li {
    margin-bottom: 3px;
}
li > ul, li > ol {
    margin-top: 3px;
    margin-bottom: 6px;
    page-break-before: avoid;
}
strong {
    color: #0f172a;
}
.mermaid-container {
    text-align: center;
    margin: 20px 0;
    page-break-inside: avoid;
}
.mermaid-container svg {
    max-width: 100%;
    height: auto;
}
.page-break {
    page-break-before: always;
}
"""


def render_mermaid(mermaid_code: str, output_path: Path) -> Path:
    """Render a mermaid diagram to SVG using mmdc."""
    import glob

    with tempfile.NamedTemporaryFile(suffix=".mmd", mode="w", delete=False) as f:
        f.write(mermaid_code)
        mmd_path = f.name

    # Find Chrome and create puppeteer config with --no-sandbox
    chrome_paths = [p for p in glob.glob("/home/*/.cache/puppeteer/**/chrome", recursive=True)
                    if Path(p).is_file()]
    if not chrome_paths:
        chrome_paths = [p for p in glob.glob("/usr/bin/chromium*") if Path(p).is_file()]
    puppeteer_config = None
    if chrome_paths:
        config_path = Path(tempfile.gettempdir()) / "puppeteer-config.json"
        config_path.write_text(
            f'{{"executablePath":"{chrome_paths[0]}","args":["--no-sandbox"]}}'
        )
        puppeteer_config = str(config_path)

    svg_path = output_path.with_suffix(".png")
    cmd = ["npx", "--yes", "@mermaid-js/mermaid-cli", "-i", mmd_path, "-o", str(svg_path),
           "-b", "white", "--width", "2400", "--scale", "3"]
    if puppeteer_config:
        cmd.extend(["-p", puppeteer_config])

    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        stderr = getattr(e, 'stderr', b'').decode() if hasattr(e, 'stderr') else str(e)
        print(f"Warning: mermaid render failed: {stderr}", file=sys.stderr)
        return None
    finally:
        Path(mmd_path).unlink(missing_ok=True)

    return svg_path


def process_markdown(md_text: str) -> str:
    """Extract mermaid blocks, render to SVG, replace with img tags."""
    mermaid_pattern = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
    svgs = []

    svg_replacements = {}

    def replace_mermaid(match):
        idx = len(svg_replacements)
        img_path = render_mermaid(match.group(1), Path(tempfile.gettempdir()) / f"mermaid_{idx}")
        placeholder = f"MERMAID_PLACEHOLDER_{idx}"
        if img_path and img_path.exists():
            import base64
            b64 = base64.b64encode(img_path.read_bytes()).decode()
            svg_replacements[placeholder] = f'<img src="data:image/png;base64,{b64}" style="max-width:100%;display:block;margin:15px auto;" />'
        else:
            svg_replacements[placeholder] = "<p><em>[Mermaid diagram could not be rendered]</em></p>"
        return f"\n\n{placeholder}\n\n"

    md_text = mermaid_pattern.sub(replace_mermaid, md_text)

    # Add page breaks before sections that should start on a new page
    md_text = re.sub(r"\n---\n\n## Scaling", r"\n\n<div class='page-break'></div>\n\n## Scaling", md_text)
    md_text = re.sub(r"\n---\n\n## Risk Mitigation", r"\n\n<div class='page-break'></div>\n\n## Risk Mitigation", md_text)

    return md_text, svg_replacements


def main():
    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)

    md_text = MD_FILE.read_text()
    md_text, svg_replacements = process_markdown(md_text)

    html_body = markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "attr_list"],
    )

    # Replace placeholders with actual SVG content
    for placeholder, svg_content in svg_replacements.items():
        html_body = html_body.replace(
            f"<p>{placeholder}</p>",
            f'<div class="mermaid-container">{svg_content}</div>'
        )

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>{CSS}</style>
</head>
<body>
{html_body}
</body>
</html>"""

    HTML(string=full_html, base_url=str(ROOT)).write_pdf(str(OUT_PDF))
    print(f"PDF generated: {OUT_PDF}")
    print(f"Size: {OUT_PDF.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
