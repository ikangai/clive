# Documentation Driver

ENVIRONMENT: bash shell for document access and generation.
WORKING DIR: /tmp/clive

PRIMARY TOOLS:
  man command              → full manual page
  man -k keyword           → search manuals by keyword
  tldr command             → concise usage examples
  pandoc in.md -o out.pdf  → document conversion
  pandoc in.md -t plain    → markdown to plain text
  wc -lwc file             → line/word/char counts
  diff file1 file2         → compare files
  fold -w 80               → wrap long lines

PATTERNS:
- Quick reference: tldr command (if installed), else man command | head -40
- Search docs: man -k 'keyword' | head -20
- Extract section: man command | sed -n '/^EXAMPLES/,/^[A-Z]/p'
- Generate report: pandoc notes.md -o report.pdf
- Concat docs: cat *.md > combined.md

PITFALLS:
- man uses pager by default: pipe through cat or head to avoid hanging
- pandoc needs texlive for PDF output: use -t html as fallback
- tldr not always installed: fall back to man

COMPLETION: Use <cmd type="task_complete">summary</cmd> when done.
Write results to /tmp/clive/.
