#!/bin/bash
# VM health check for c3po-vm.exe.xyz — startup step S5a (CLAUDE.md "Session rituals").
# Silent when clean: prints one "VM OK" line, or one "!!" line per problem. Read-only: changes nothing.
#
#   bin/vm_health.sh            # from the laptop clone
#
# Checks: services active; daemon/bot code newer than the running process (needs a restart);
# ~/c3po dirty / unpushed / diverged; failed daemon steps and push/PR errors in the last 24 h;
# last cycle completion; ~/website dirt that is not explained by the open auto-PR; disk > 80%;
# scoped GitHub PATs near their expiry.

VM=c3po-vm.exe.xyz
PAT_EXPIRY=2026-12-08     # the VM's fine-grained PATs (CLAUDE.md security notice); update on rotation
PAT_WARN_DAYS=30

out=$(ssh -o ConnectTimeout=20 "$VM" 'bash -s' <<'REMOTE'
p() { echo "!! $*"; }
for s in c3po-daemon c3po-bot; do
  [ "$(systemctl is-active $s)" = active ] || p "$s is $(systemctl is-active $s)"
done
cd ~/c3po || { p "~/c3po missing"; exit; }
git fetch -q 2>/dev/null || p "c3po: git fetch failed"
dirty=$(git status --porcelain | wc -l)
[ "$dirty" -gt 0 ] && p "c3po: $dirty uncommitted file(s): $(git status --porcelain | head -5 | awk '{print $2}' | tr '\n' ' ')"
ahead=$(git rev-list --count @{u}.. 2>/dev/null); behind=$(git rev-list --count ..@{u} 2>/dev/null)
[ "${ahead:-0}" -gt 0 ] && p "c3po: $ahead unpushed commit(s) on the VM (stranded daemon commits?)"
[ "${behind:-0}" -gt 3 ] && p "c3po: $behind commits behind origin (daemon self-pull not keeping up?)"
for pair in "c3po-daemon:bin/daemon.py" "c3po-bot:bin/c3po_bot.py"; do
  svc=${pair%%:*}; f=${pair#*:}
  started=$(date -d "$(systemctl show $svc -p ActiveEnterTimestamp --value)" +%s 2>/dev/null)
  changed=$(git log -1 --format=%ct -- "$f" 2>/dev/null)
  [ -n "$started" ] && [ -n "$changed" ] && [ "$changed" -gt "$started" ] && \
    p "$svc: $f changed $(date -d @$changed +%F) after the service started — needs: sudo systemctl restart $svc"
done
log=$(sudo journalctl -u c3po-daemon --since "24 hours ago" --no-pager 2>/dev/null)
fails=$(echo "$log" | grep -E "✗ .* (failed|timed out|error)" | sed -E 's/.*✗ //' | sort | uniq -c | sort -rn | head -5)
[ -n "$fails" ] && p "daemon step failures (24h): $(echo "$fails" | awk '{$1=$1; print}' | tr '\n' ';')"
errs=$(echo "$log" | grep -E "push failed|PR flow failed|still dirty|crashed|aborting" | tail -3 | sed -E 's/^.*(INFO|WARNING|ERROR) *//')
[ -n "$errs" ] && p "daemon errors (24h): $(echo "$errs" | tr '\n' ';')"
last=$(echo "$log" | grep "Sync cycle .* complete" | tail -1)
if [ -z "$last" ]; then p "no completed daemon cycle in the last 24h"
else
  # "N/M steps OK": conditional steps (e.g. the gated resource syncs) can make N exceed M; only N < M is a failure.
  okn=$(echo "$last" | sed -nE 's/.*complete — ([0-9]+)\/([0-9]+) steps OK.*/\1 \2/p')
  [ -n "$okn" ] && [ "${okn% *}" -ge "${okn#* }" ] || p "last cycle not clean: $(echo "$last" | sed -E 's/.*(Sync cycle)/\1/')"
fi
bot=$(sudo journalctl -u c3po-bot --since "24 hours ago" --no-pager 2>/dev/null | grep -ciE "traceback|exception")
[ "$bot" -gt 0 ] && p "c3po-bot: $bot traceback/exception line(s) in 24h"
if cd ~/website 2>/dev/null; then
  git fetch -q 2>/dev/null
  [ "$(git branch --show-current)" = main ] || p "website clone on branch $(git branch --show-current), expected main"
  git status --porcelain | awk '{print $2}' > /tmp/website_dirty.txt
  echo "WEBSITE_DIRTY $(wc -l < /tmp/website_dirty.txt)"
  cat /tmp/website_dirty.txt | sed 's/^/WD /'
fi
use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "$use" -gt 80 ] && p "disk ${use}% used"
echo REMOTE_DONE
REMOTE
)
echo "$out" | grep -q REMOTE_DONE || { echo "!! ssh to $VM failed or the remote check did not finish"; exit 1; }

problems=$(echo "$out" | grep '^!!')

# Website dirt is expected only when it matches the open auto-PR (the daemon regenerates pages in the
# working tree and pushes them to c3po/auto-sig-pages). Anything else needs a look.
nd=$(echo "$out" | sed -n 's/^WEBSITE_DIRTY //p')
if [ "${nd:-0}" -gt 0 ]; then
  pr_files=$(gh pr list --repo Protocol-Institute/website --head c3po/auto-sig-pages --state open \
               --json files -q '.[].files[].path' 2>/dev/null)
  unexplained=$(echo "$out" | sed -n 's/^WD //p' | while read -r f; do
    f=${f%/}; echo "$pr_files" | grep -q "^$f" || echo "$f"; done)
  if [ -n "$unexplained" ]; then
    problems+=$'\n'"!! website clone: $(echo "$unexplained" | wc -l | tr -d ' ') dirty path(s) not in an open auto-PR: $(echo "$unexplained" | head -5 | tr '\n' ' ')"
  else
    pr=$(gh pr list --repo Protocol-Institute/website --head c3po/auto-sig-pages --state open --json number,createdAt -q '.[0] | "#\(.number) (\(.createdAt[:10]))"')
    info="website clone dirt = open auto-PR $pr, awaiting review"
  fi
fi

days=$(( ( $(date -j -f %F "$PAT_EXPIRY" +%s) - $(date +%s) ) / 86400 ))
[ "$days" -le "$PAT_WARN_DAYS" ] && problems+=$'\n'"!! VM GitHub PATs expire $PAT_EXPIRY ($days days) — rotate (CLAUDE.md security notice)"

problems=$(echo "$problems" | sed '/^$/d')
if [ -z "$problems" ]; then echo "VM OK${info:+ — $info}"; else echo "$problems"; [ -n "$info" ] && echo "   ($info)"; fi
