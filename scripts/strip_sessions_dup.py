"""Remove duplicate session helpers from routes/sessions.py (S12)."""
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "backend" / "routes" / "sessions.py"
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
start = next(i for i, l in enumerate(lines) if l.startswith("async def build_sheet_context"))
end = next(i for i, l in enumerate(lines) if '"/api/session"' in l and "@router.post" in l)
new_lines = lines[:start] + lines[end:]
p.write_text("".join(new_lines), encoding="utf-8")
print(f"Removed lines {start + 1}-{end}, new total {len(new_lines)}")
