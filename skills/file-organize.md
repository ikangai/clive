# File Organize

PROCEDURE:
1. Survey: `ls -la` and `du -sh *` to understand what's there
2. Identify file types: `file * | sort -t: -k2`
3. Count by extension: `find . -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn`
4. Find large files: `find . -size +1M -exec ls -lh {} \;`
5. Find duplicates (by size): `find . -type f -exec md5sum {} \; | sort | uniq -d -w32`
6. Propose organization and execute (mkdir categories, mv files)
7. Write manifest to session_dir/organized.json
8. task_complete with summary

TIPS:
- Don't move files without confirming the plan first
- Preserve directory structure when possible
- Use -n flag with mv to avoid overwrites
