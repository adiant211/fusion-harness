# Tools
- `bash` runs in the workspace. Pass timeout_s for long commands. Read the exit code first.
- Read a file before editing it; `edit_file` needs the exact existing text. Use `write_file` for
  new or fully rewritten files.
- `find_text` searches the workspace for a regex and returns path:line matches. Use it to locate
  code before reading files one by one.
- Harness scripts, when there are any, live in $FH_HARNESS/scripts/.
