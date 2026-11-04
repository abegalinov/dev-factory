#!/bin/sh
# One-screen status of a Dev Factory installed by install-factory.sh: the timer, the last
# poll, recent problems, agent containers, memory and disk, and open issues by stage.
# Read-only.
#   sudo sh /opt/dev-factory/deploy/host/factory-status.sh [--since "6 hours ago"]
set -u

APP=/opt/dev-factory
DF_HOME=/var/lib/dev-factory
SINCE="24 hours ago"

die() { echo "error: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo): it reads the journal and devfactory's gh login"

as_df() { (cd "$APP" && runuser -u devfactory -- env HOME="$DF_HOME" "$@"); }
size() { numfmt --to=iec "$1" 2>/dev/null || echo "$1"; }

# Reads the poll unit's journal (-o cat): the last JSON report the poll printed, and the
# WARNING/ERROR log lines.
JOURNAL_SUMMARY=$(cat <<'EOF'
import json, sys

lines = sys.stdin.read().splitlines()
start = max((i for i, line in enumerate(lines) if line == "{"), default=None)
report = None
if start is not None:
    try:
        end = lines.index("}", start)
        report = json.loads("\n".join(lines[start:end + 1]))
    except ValueError:
        pass

print("== last poll")
if report is None:
    print(f"  no poll report since {sys.argv[1]}")
else:
    print("  mode: " + ("observe-only" if report.get("observe_only") else "mutating"))
    issues = report.get("issues", [])
    if not issues:
        print("  no labelled issues")
    for item in issues:
        line = f"  {item['project_id']}#{item['issue']} [{item.get('stage') or '-'}] {item['action']}"
        if item.get("target"):
            line += f" {item['target']}"
        line += f": {item['reason']}"
        if item.get("outcome"):
            line += f" -> {item['outcome']}"
        print(line)
    for error in report.get("errors", []):
        print(f"  ERROR {error}")

problems, failed_runs = [], 0
for line in lines:
    if "Failed with result" in line:
        failed_runs += 1
    if not line.startswith('{"'):
        continue
    try:
        entry = json.loads(line)
    except ValueError:
        continue
    if entry.get("level") in ("WARNING", "ERROR", "CRITICAL"):
        problems.append(entry)

print(f"== problems since {sys.argv[1]}")
print(f"  failed polls: {failed_runs}, warnings/errors logged: {len(problems)}")
for entry in problems[-5:]:
    print(f"  {entry.get('ts', '?')} {entry['level']} {entry.get('msg', '')}"[:200])
EOF
)

# Groups open issues by their factory:* label; the stages marked "you" wait for a human.
ISSUE_SUMMARY=$(cat <<'EOF'
import json, sys

human = {"factory:spec-review", "factory:review", "factory:release",
         "factory:needs-input", "factory:blocked"}
by_label = {}
for issue in json.load(sys.stdin):
    for label in issue["labels"]:
        if label["name"].startswith("factory:"):
            by_label.setdefault(label["name"], []).append(issue)
if not by_label:
    print("  no open factory issues")
for label in sorted(by_label):
    mark = "  <- you" if label in human else ""
    print(f"  {label}{mark}")
    for issue in by_label[label]:
        print(f"    #{issue['number']} {issue['title']}"[:120])
EOF
)

echo "== timer"
systemctl list-timers dev-factory-poll.timer --no-pager | head -n 2
state=$(systemctl show dev-factory-poll.service -p ActiveState --value)
result=$(systemctl show dev-factory-poll.service -p Result --value)
echo "  poll service: $state (last result: $result)"
grep -h "observe_only:" "$APP/config/factory.yaml" | sed 's/^ */  /'

echo "== credentials"
if as_df gh auth status >/dev/null 2>&1; then
  echo "  gh: $(as_df gh api user --jq .login 2>/dev/null || echo 'logged in')"
else
  echo "  gh: NOT LOGGED IN (sudo sh $APP/deploy/host/install-factory.sh --gh-user LOGIN)"
fi
# Only checks that the file exists; its contents are a credential.
if [ -s "$DF_HOME/.config/dev-factory/agent.env" ]; then
  echo "  agent.env: present"
else
  echo "  agent.env: MISSING (agents will report 'Not logged in')"
fi

journalctl -u dev-factory-poll -o cat --since "$SINCE" --no-pager 2>/dev/null \
  | python3 -c "$JOURNAL_SUMMARY" "$SINCE"

echo "== agent containers"
running=$(docker ps --filter name=dev-factory- --format '{{.Names}}' | grep -v '^dev-factory-deployer$')
if [ -n "$running" ]; then
  # shellcheck disable=SC2086
  docker stats --no-stream --format '  {{.Name}} cpu {{.CPUPerc}} mem {{.MemUsage}}' $running
else
  echo "  none running"
fi

echo "== memory and disk"
current=$(systemctl show dev-factory.slice -p MemoryCurrent --value)
high=$(systemctl show dev-factory.slice -p MemoryHigh --value)
max=$(systemctl show dev-factory.slice -p MemoryMax --value)
echo "  factory slice: $(size "$current") (high $(size "$high"), max $(size "$max"))"
free -h | awk 'NR==2 {print "  host memory: " $3 " used, " $7 " available of " $2}
               NR==3 {print "  host swap: " $3 " used of " $2}'
echo "  runtime: $(du -sh "$APP/.runtime" 2>/dev/null | cut -f1), disk: $(df -h / | awk 'NR==2 {print $4 " free of " $2}')"

echo "== open issues"
for file in "$APP"/config/projects/*.yaml; do
  grep -q "^enabled: true" "$file" || continue
  repo=$(sed -n 's/^repository: *//p' "$file")
  echo "  $repo"
  as_df gh issue list -R "$repo" --state open --limit 300 --json number,title,labels \
    | python3 -c "$ISSUE_SUMMARY" | sed 's/^/  /' \
    || echo "    could not list issues"
done
