# Backup

PROCEDURE:
1. Survey target: `du -sh TARGET` to check size
2. Choose method based on size:
   - < 100MB: tar -czf backup.tar.gz TARGET
   - > 100MB: tar -cf - TARGET | pigz > backup.tar.gz (parallel compression)
   - Remote: rsync -avz TARGET DEST
3. Verify: `tar -tzf backup.tar.gz | wc -l` to count files
4. Record metadata: date, size, file count, checksum
5. Write manifest to session_dir/backup_manifest.json
6. task_complete with backup location and stats

TIPS:
- Exclude .git and node_modules: --exclude='.git' --exclude='node_modules'
- Add timestamp to filename: backup_$(date +%Y%m%d_%H%M%S).tar.gz
