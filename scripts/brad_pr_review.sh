#!/bin/bash
set -euo pipefail

BRAD_BOT_TAG="**Brad Reviewer**"
CODEX_TIMEOUT=1800

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  shift
fi

if [[ "$DRY_RUN" == true ]]; then
  PR_NUMBER=""
  BASE_BRANCH="${1:-main}"
else
  PR_NUMBER="${1:-}"
  BASE_BRANCH="${2:-main}"
  if [[ -z "$PR_NUMBER" ]]; then
    echo "Usage: $0 [--dry-run] <pr_number> [base_branch]"
    echo "       $0 --dry-run [base_branch]"
    exit 1
  fi
fi

group()    { if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::group::$1"; else echo "--- $1 ---"; fi; }
endgroup() { if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::endgroup::"; fi; }
warn()     { if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::warning::$1"; else echo "WARN: $1"; fi; }

echo "=== Brad AI PR Review ==="
echo "PR: #${PR_NUMBER:-local}  Base: $BASE_BRANCH  Dry-run: $DRY_RUN"

GITHUB_API="https://api.github.com/repos/${GITHUB_REPOSITORY:-unset}"
CURL_RETRY_OPTS=(--retry 5 --retry-delay 2 --retry-all-errors --retry-connrefused --max-time 60)
gh_api() { curl -fsSL "${CURL_RETRY_OPTS[@]}" -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github.v3+json" "$@"; }
gh_post() { curl -fsSL "${CURL_RETRY_OPTS[@]}" -X POST -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github.v3+json" "$@"; }

ensure_git_ref() {
  local ref="$1"
  local fetch_spec="${2:-}"
  if git rev-parse --verify "$ref" >/dev/null 2>&1; then
    return 0
  fi
  if [[ -z "$fetch_spec" ]]; then
    return 1
  fi
  git fetch --no-tags --prune origin "$fetch_spec"
  git rev-parse --verify "$ref" >/dev/null 2>&1
}

PR_INFO=""
PR_HEAD_SHA=""
REREVIEW_REQUESTED=false
if [[ "$DRY_RUN" == false ]]; then
  PR_INFO=$(gh_api "$GITHUB_API/pulls/$PR_NUMBER")
  PR_HEAD_SHA=$(echo "$PR_INFO" | jq -r '.head.sha')
fi

if [[ "$DRY_RUN" == false ]]; then
  group "Check review state"

  COMMENTS_JSON=$(gh_api "$GITHUB_API/issues/$PR_NUMBER/comments?per_page=100")
  REVIEWS_JSON=$(gh_api "$GITHUB_API/pulls/$PR_NUMBER/reviews?per_page=100")

  brad_has_review() {
    echo "$REVIEWS_JSON" | jq -e '[.[] | select(((.body // "") | startswith("**Brad** (AI Reviewer)")) or ((.body // "") | startswith("**Brad Reviewer**")))] | length > 0' >/dev/null 2>&1
  }

  has_unanswered_rereview() {
    local trigger_ids
    trigger_ids=$(echo "$COMMENTS_JSON" | jq -r '[.[] | select(.body | test("(?i)brad:?\\s*re-?review")) | .id] | .[]')
    [[ -z "$trigger_ids" ]] && return 1
    local reply_bodies
    reply_bodies=$(echo "$COMMENTS_JSON" | jq -r '[.[] | select(.body | startswith("Re-reviewing")) | .body] | .[]')
    for tid in $trigger_ids; do
      if ! echo "$reply_bodies" | grep -qF "re-review:$tid"; then
        echo "$tid"
        return 0
      fi
    done
    return 1
  }

  if brad_has_review; then
    if TRIGGER_COMMENT_ID=$(has_unanswered_rereview); then
      REREVIEW_REQUESTED=true
      echo "Re-review requested (trigger comment $TRIGGER_COMMENT_ID)"
      gh_post "$GITHUB_API/issues/$PR_NUMBER/comments" \
        -d "$(jq -n --arg b "Re-reviewing (re-review:$TRIGGER_COMMENT_ID)" '{body:$b}')" >/dev/null
    else
      echo "Brad already reviewed this PR. No re-review requested. Skipping."
      exit 0
    fi
  else
    echo "First review for this PR."
  fi

  endgroup
fi

if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  group "Azure OpenAI setup"

  AZURE_OPENAI_RESOURCE_NAME="${AZURE_OPENAI_RESOURCE_NAME:-test-flaerobotics-openai-sweden-central}"
  AZURE_OPENAI_RESOURCE_GROUP="${AZURE_OPENAI_RESOURCE_GROUP:-TEST}"
  AZURE_OPENAI_BASE_URL="${AZURE_OPENAI_BASE_URL:-https://${AZURE_OPENAI_RESOURCE_NAME}.cognitiveservices.azure.com/openai/v1}"
  CODEX_MODEL="${CODEX_MODEL:-gpt-5.4}"

  AZURE_OPENAI_KEY=$(az cognitiveservices account keys list \
    --name "$AZURE_OPENAI_RESOURCE_NAME" \
    --resource-group "$AZURE_OPENAI_RESOURCE_GROUP" \
    --query key1 -o tsv)
  export AZURE_OPENAI_API_KEY="$AZURE_OPENAI_KEY"

  mkdir -p ~/.codex
  cat > ~/.codex/config.toml <<TOML
model = "$CODEX_MODEL"
model_provider = "azure"

[model_providers.azure]
name = "Azure OpenAI"
base_url = "$AZURE_OPENAI_BASE_URL"
env_key = "AZURE_OPENAI_API_KEY"
TOML
  echo "Configured Azure OpenAI provider (resource: $AZURE_OPENAI_RESOURCE_NAME)"
  endgroup
fi

group "Run Codex review"

PR_TITLE="${PR_TITLE:-}"
PR_DESC="${PR_DESC:-}"
if [[ "$DRY_RUN" == false ]]; then
  PR_TITLE=$(echo "$PR_INFO" | jq -r '.title')
  PR_DESC=$(echo "$PR_INFO" | jq -r '.body // ""')
fi

JIRA_TICKET_ID=$(echo "$PR_TITLE $PR_DESC" | grep -oE 'DEV-[0-9]+' | head -1 || true)
JIRA_DESCRIPTION=""
JIRA_SUMMARY=""

if [[ -n "$JIRA_TICKET_ID" && -n "${BRAD_REVIEW_JIRA_API_TOKEN:-}" ]]; then
  group "Fetch Jira issue $JIRA_TICKET_ID"
  JIRA_AUTH=$(printf '%s' "brad@flaerobotics.ai:${BRAD_REVIEW_JIRA_API_TOKEN}" | base64 -w 0)
  JIRA_JSON=$(curl -fsSL --max-time 15 \
    -H "Authorization: Basic $JIRA_AUTH" \
    -H "Accept: application/json" \
    "https://flaerobotics.atlassian.net/rest/api/3/issue/${JIRA_TICKET_ID}?fields=summary,description" 2>/dev/null || true)
  
  if [[ -n "$JIRA_JSON" ]]; then
    JIRA_SUMMARY=$(printf '%s' "$JIRA_JSON" | jq -r '.fields.summary // ""')
    JIRA_DESCRIPTION=$(printf '%s' "$JIRA_JSON" | python3 -c "
import json, sys
def text(n):
    if not n:
        return ''
    if isinstance(n, str):
        return n
    if isinstance(n, list):
        return ''.join(text(x) for x in n)
    if not isinstance(n, dict):
        return ''
    t = n.get('type')
    if t == 'text':
        return n.get('text', '')
    if t == 'hardBreak':
        return '\n'
    c = ''.join(text(x) for x in n.get('content', []))
    return (c + '\n') if t in ('paragraph','heading','blockquote','listItem') and c else c
d = json.load(sys.stdin)
print(text(d.get('fields',{}).get('description') or '').strip())
" 2>/dev/null || true)
    echo "Fetched Jira $JIRA_TICKET_ID: $JIRA_SUMMARY (description: ${#JIRA_DESCRIPTION} chars)"
  else
    echo "Could not fetch Jira issue $JIRA_TICKET_ID (continuing without Jira context)"
  fi
  endgroup
fi

CODEX_LAST_MESSAGE_FILE="codex-last-message.md"
CODEX_STDOUT_FILE="codex-stdout.txt"
BATCH_PLAN_FILE="brad-batches.json"
STATE_FILE="${BRAD_REVIEW_STATE_FILE:-.brad-review-state.json}"
RETRY_LIMIT_RAW="${BRAD_BATCH_MAX_RETRIES:-${BRAD_MAX_RETRIES:-3}}"

if [[ ! "$RETRY_LIMIT_RAW" =~ ^[0-9]+$ ]] || [[ "$RETRY_LIMIT_RAW" -lt 1 ]]; then
  warn "Invalid BRAD_BATCH_MAX_RETRIES='$RETRY_LIMIT_RAW', defaulting to 3"
  RETRY_LIMIT=3
else
  RETRY_LIMIT=$RETRY_LIMIT_RAW
fi

build_state_from_batches() {
  local state_pr_number="$1"
  local state_head_sha="$2"
  local diff_range="$3"
  jq -n \
    --argjson pr_number "$state_pr_number" \
    --arg base_branch "$BASE_BRANCH" \
    --arg head_sha "$state_head_sha" \
    --arg diff_range "$diff_range" \
    --argjson retry_limit "$RETRY_LIMIT" \
    --slurpfile plan "$BATCH_PLAN_FILE" '
      {
        schema_version: 2,
        pr_number: $pr_number,
        base_branch: $base_branch,
        head_sha: $head_sha,
        diff_range: $diff_range,
        retry_limit: $retry_limit,
        totals: {
          tokens: 0,
          inline_comments_posted: 0
        },
        batches: ($plan[0].batches | map({
          index: .index,
          status: "pending",
          attempts: 0,
          tokens: 0,
          changed_lines: .changed_lines,
          files: (.files | map(.path)),
          last_error: null,
          result_path: null
        })),
        findings: {},
        posted_findings: {}
      }
    '
}

apply_state_update() {
  local tmp_file
  tmp_file=$(mktemp)
  if jq "$@" "$STATE_FILE" >"$tmp_file"; then
    mv "$tmp_file" "$STATE_FILE"
    return 0
  fi
  rm -f "$tmp_file"
  return 1
}

extract_token_count() {
  local stderr_file="$1"
  local token_value
  token_value=$(awk '
    /^tokens used$/ {
      getline
      print
      exit
    }
    /^tokens used[[:space:]]+[0-9,]+$/ {
      sub(/^tokens used[[:space:]]+/, "")
      print
      exit
    }
  ' "$stderr_file")
  token_value=$(printf '%s' "$token_value" | tr -d '[:space:],')
  if [[ "$token_value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$token_value"
  else
    printf '0\n'
  fi
}

echo "Diff stats:"
BASE_REF="refs/remotes/origin/$BASE_BRANCH"
if ! ensure_git_ref "$BASE_REF" "$BASE_BRANCH:$BASE_REF"; then
  warn "Base branch origin/$BASE_BRANCH is not available for diff."
  printf '%s\n' "Review failed: base branch origin/$BASE_BRANCH is unavailable in this runner checkout." >"$CODEX_LAST_MESSAGE_FILE"
  REVIEW_OUTPUT=$(cat "$CODEX_LAST_MESSAGE_FILE")
  REVIEW_DURATION=0
  TOKEN_COUNT="unknown"
  endgroup
else
  if [[ "$DRY_RUN" == false ]]; then
    PR_REF="refs/remotes/origin/pr/$PR_NUMBER"
    if ! ensure_git_ref "$PR_REF" "pull/$PR_NUMBER/head:$PR_REF"; then
      warn "PR ref refs/remotes/origin/pr/$PR_NUMBER is not available for diff."
      printf '%s\n' "Review failed: PR ref for #$PR_NUMBER is unavailable in this runner checkout." >"$CODEX_LAST_MESSAGE_FILE"
      REVIEW_OUTPUT=$(cat "$CODEX_LAST_MESSAGE_FILE")
      REVIEW_DURATION=0
      TOKEN_COUNT="unknown"
      endgroup
      FOOTER="_Review completed in ${REVIEW_DURATION}s | tokens: ${TOKEN_COUNT}_"
      BODY=$(printf '%s\n\n%s\n\n---\n%s' "$BRAD_BOT_TAG" "$REVIEW_OUTPUT" "$FOOTER")
      printf '%s\n' "$BODY"
      exit 1
    fi
    DIFF_RANGE="$BASE_REF...$PR_REF"
  else
    DIFF_RANGE="$BASE_REF...HEAD"
  fi

  git diff --stat "$DIFF_RANGE"

  if [[ "$DRY_RUN" == false ]]; then
    python3 ./scripts/brad_batch_review.py --pr "$PR_NUMBER" --base "$BASE_BRANCH" --format json >"$BATCH_PLAN_FILE"
  else
    python3 ./scripts/brad_batch_review.py --base "$BASE_BRANCH" --format json >"$BATCH_PLAN_FILE"
  fi

  if [[ "$DRY_RUN" == false ]]; then
    STATE_PR_NUMBER="$PR_NUMBER"
    STATE_HEAD_SHA="$PR_HEAD_SHA"
  else
    STATE_PR_NUMBER="0"
    STATE_HEAD_SHA=$(git rev-parse HEAD)
  fi

  if [[ -s "$STATE_FILE" ]]; then
    if [[ "$REREVIEW_REQUESTED" == true ]]; then
      echo "Explicit re-review requested; resetting cached Brad state"
      build_state_from_batches "$STATE_PR_NUMBER" "$STATE_HEAD_SHA" "$DIFF_RANGE" >"$STATE_FILE"
    elif ! jq -e \
      --arg base_branch "$BASE_BRANCH" \
      --arg head_sha "$STATE_HEAD_SHA" \
      --arg diff_range "$DIFF_RANGE" \
      --argjson pr_number "$STATE_PR_NUMBER" '
        .schema_version == 2
        and .base_branch == $base_branch
        and .head_sha == $head_sha
        and .diff_range == $diff_range
        and .pr_number == $pr_number
      ' "$STATE_FILE" >/dev/null; then
      warn "Existing state file is incompatible with current review context, reinitializing"
      build_state_from_batches "$STATE_PR_NUMBER" "$STATE_HEAD_SHA" "$DIFF_RANGE" >"$STATE_FILE"
    fi
  else
    build_state_from_batches "$STATE_PR_NUMBER" "$STATE_HEAD_SHA" "$DIFF_RANGE" >"$STATE_FILE"
  fi

  GLOBAL_CHANGED_FILES=$(git diff --name-only "$DIFF_RANGE")
  REVIEW_START=$(date +%s)

  mapfile -t PENDING_BATCH_INDEXES < <(jq -r '.batches[] | select(.status != "completed") | .index' "$STATE_FILE")
  for batch_index in "${PENDING_BATCH_INDEXES[@]}"; do
    batch_idx0=$((batch_index - 1))
    batch_attempts=$(jq -r --argjson idx "$batch_idx0" '.batches[$idx].attempts' "$STATE_FILE")

    while (( batch_attempts < RETRY_LIMIT )); do
      batch_attempts=$((batch_attempts + 1))
      apply_state_update \
        --argjson idx "$batch_idx0" \
        --argjson attempts "$batch_attempts" \
        '.batches[$idx].status = "in_progress" | .batches[$idx].attempts = $attempts | .batches[$idx].last_error = null'

      mapfile -t BATCH_FILES < <(jq -r --argjson idx "$batch_idx0" '.batches[$idx].files[]' "$STATE_FILE")
      if [[ ${#BATCH_FILES[@]} -eq 0 ]]; then
        apply_state_update --argjson idx "$batch_idx0" '.batches[$idx].status = "completed"'
        break
      fi

      batch_primary_file="${BATCH_FILES[0]}"
      batch_files_block=$(printf '%s\n' "${BATCH_FILES[@]}")
      batch_diff=$(git diff "$DIFF_RANGE" -- "${BATCH_FILES[@]}")
      reviewed_summary=$(jq -r '
        (.findings // {})
        | to_entries
        | .[:40]
        | if length == 0 then "- none"
          else map("- [\(.value.severity)] \(.value.path):\(.value.line) \(.value.message)") | join("\\n")
          end
      ' "$STATE_FILE")

      JIRA_CONTEXT=""
      if [[ -n "$JIRA_TICKET_ID" ]]; then
        JIRA_CONTEXT="Jira Requirements ($JIRA_TICKET_ID: $JIRA_SUMMARY):
$JIRA_DESCRIPTION

IMPORTANT: Validate the implementation against these Jira requirements and flag any gaps or missing functionality.
"
      fi

      DEFAULT_REVIEW_PROMPT="You are Brad, an expert senior software engineer reviewing pull request code changes.

PR title: ${PR_TITLE:-local review}
PR description: ${PR_DESC:-n/a}
Batch index: ${batch_index}
Primary focus file: ${batch_primary_file}

Global changed files:
${GLOBAL_CHANGED_FILES}

Current batch files:
${batch_files_block}

Already posted findings summary:
${reviewed_summary}

${JIRA_CONTEXT}
Batch diff:
${batch_diff}

Return ONLY valid JSON (no markdown, no fences) with this schema:
{
  \"batch_index\": <int>,
  \"findings\": [
    {
      \"path\": \"relative/path.py\",
      \"line\": <int>,
      \"severity\": \"critical|high|medium|low\",
      \"message\": \"concise finding\"
    }
  ],
  \"notes\": \"optional concise notes\"
}

Rules:
- Do not repeat previously posted findings unless materially different.
- Only include findings tied to changed files.
- Treat the supplied changed-file list and batch diff as authoritative review input.
- Do not use the current local checkout state as a substitute for the supplied diff.
- Keep findings concrete and actionable."

      if [[ -n "${BRAD_PROMPT_OVERRIDE:-}" ]]; then
        REVIEW_PROMPT="$BRAD_PROMPT_OVERRIDE"
        echo "Using prompt override from workflow input"
      elif [[ -n "${BRAD_PROMPT_BASE:-}" ]]; then
        REVIEW_PROMPT="$BRAD_PROMPT_BASE"
        echo "Using prompt base from repository variable"
      else
        REVIEW_PROMPT="$DEFAULT_REVIEW_PROMPT"
      fi

      if [[ -n "${BRAD_PROMPT_APPEND:-}" ]]; then
        REVIEW_PROMPT="${REVIEW_PROMPT}

${BRAD_PROMPT_APPEND}"
      fi

      batch_result_file="codex-batch-${batch_index}-attempt-${batch_attempts}.json"
      batch_stdout_file="codex-batch-${batch_index}-attempt-${batch_attempts}.stdout.txt"
      batch_stderr_file="codex-batch-${batch_index}-attempt-${batch_attempts}.stderr.txt"

      echo "Running codex exec for batch ${batch_index} (attempt ${batch_attempts}/${RETRY_LIMIT})..."
      batch_rc=0
      timeout "$CODEX_TIMEOUT" codex exec --dangerously-bypass-approvals-and-sandbox --ephemeral --output-last-message "$batch_result_file" "$REVIEW_PROMPT" >"$batch_stdout_file" 2>"$batch_stderr_file" || batch_rc=$?

      batch_tokens=$(extract_token_count "$batch_stderr_file")
      apply_state_update \
        --argjson idx "$batch_idx0" \
        --argjson tokens "$batch_tokens" \
        '.batches[$idx].tokens = (.batches[$idx].tokens + $tokens) | .totals.tokens = (.totals.tokens + $tokens)'

      if [[ $batch_rc -ne 0 ]]; then
        if [[ $batch_rc -eq 124 ]]; then
          batch_error="Codex timed out after ${CODEX_TIMEOUT}s"
        else
          batch_error="Codex failed (exit ${batch_rc})"
        fi
        warn "$batch_error"
        echo "----- codex stderr (batch ${batch_index} attempt ${batch_attempts}) -----"
        tail -n 200 "$batch_stderr_file" || true
        echo "----- codex stdout (batch ${batch_index} attempt ${batch_attempts}) -----"
        tail -n 100 "$batch_stdout_file" || true
        echo "----- end codex output -----"
        apply_state_update \
          --argjson idx "$batch_idx0" \
          --arg last_error "$batch_error" \
          '.batches[$idx].status = "pending" | .batches[$idx].last_error = $last_error'
        continue
      fi

      if [[ ! -s "$batch_result_file" ]]; then
        batch_error="Codex did not produce findings output"
        warn "$batch_error"
        apply_state_update \
          --argjson idx "$batch_idx0" \
          --arg last_error "$batch_error" \
          '.batches[$idx].status = "pending" | .batches[$idx].last_error = $last_error'
        continue
      fi

      if ! jq -e '.findings and (.findings | type == "array")' "$batch_result_file" >/dev/null 2>&1; then
        batch_error="Codex output is not valid findings JSON"
        warn "$batch_error"
        apply_state_update \
          --argjson idx "$batch_idx0" \
          --arg last_error "$batch_error" \
          '.batches[$idx].status = "pending" | .batches[$idx].last_error = $last_error'
        continue
      fi

      while IFS= read -r finding_json; do
        [[ -z "$finding_json" ]] && continue
        finding_path=$(echo "$finding_json" | jq -r '.path // ""')
        finding_line=$(echo "$finding_json" | jq -r '.line // 0')
        finding_severity=$(echo "$finding_json" | jq -r '.severity // "medium" | ascii_downcase')
        finding_message=$(echo "$finding_json" | jq -r '.message // ""')

        [[ -z "$finding_path" ]] && continue
        [[ "$finding_line" =~ ^[0-9]+$ ]] || continue
        [[ -z "$finding_message" ]] && continue

        if ! printf '%s\n' "$GLOBAL_CHANGED_FILES" | grep -Fxq "$finding_path"; then
          continue
        fi

        finding_fp=$(printf '%s|%s|%s|%s|%s' "$STATE_HEAD_SHA" "$finding_path" "$finding_line" "$finding_severity" "$finding_message" | sha256sum | cut -d' ' -f1)

        apply_state_update \
          --arg fp "$finding_fp" \
          --arg path "$finding_path" \
          --argjson line "$finding_line" \
          --arg severity "$finding_severity" \
          --arg message "$finding_message" '
            .findings[$fp] = ((.findings[$fp] // {}) + {
              path: $path,
              line: $line,
              severity: $severity,
              message: $message
            })
          '

        if [[ "$DRY_RUN" == true ]]; then
          continue
        fi

        if jq -e --arg fp "$finding_fp" '.posted_findings[$fp] != null' "$STATE_FILE" >/dev/null; then
          continue
        fi

        inline_body="**${finding_severity^^}** ${finding_path}:${finding_line} - ${finding_message}
<!-- brad-fp:${finding_fp} -->"
        inline_payload=$(jq -n \
          --arg body "$inline_body" \
          --arg path "$finding_path" \
          --arg commit_id "$PR_HEAD_SHA" \
          --argjson line "$finding_line" \
          '{body:$body, path:$path, commit_id:$commit_id, line:$line, side:"RIGHT"}')

        if comment_response=$(gh_post "$GITHUB_API/pulls/$PR_NUMBER/comments" -d "$inline_payload" 2>/dev/null); then
          comment_id=$(echo "$comment_response" | jq -r '.id // 0')
          apply_state_update \
            --arg fp "$finding_fp" \
            --arg path "$finding_path" \
            --argjson line "$finding_line" \
            --arg severity "$finding_severity" \
            --arg message "$finding_message" \
            --argjson comment_id "$comment_id" '
              .findings[$fp] = ((.findings[$fp] // {}) + {
                path: $path,
                line: $line,
                severity: $severity,
                message: $message,
                comment_id: $comment_id
              })
              | .posted_findings[$fp] = {
                  path: $path,
                  line: $line,
                  severity: $severity,
                  message: $message,
                  comment_id: $comment_id
                }
              | .totals.inline_comments_posted = (.totals.inline_comments_posted + 1)
            '
        else
          warn "Failed to post inline comment for ${finding_path}:${finding_line}"
        fi
      done < <(jq -c '.findings[]?' "$batch_result_file")

      apply_state_update \
        --argjson idx "$batch_idx0" \
        --arg result_path "$batch_result_file" '
          .batches[$idx].status = "completed"
          | .batches[$idx].last_error = null
          | .batches[$idx].result_path = $result_path
        '

      break
    done

    batch_status=$(jq -r --argjson idx "$batch_idx0" '.batches[$idx].status' "$STATE_FILE")
    if [[ "$batch_status" != "completed" ]]; then
      apply_state_update --argjson idx "$batch_idx0" '.batches[$idx].status = "failed_transient"'
      warn "Batch ${batch_index} exhausted retries (${RETRY_LIMIT})"
    fi
  done

  REVIEW_DURATION=$(( $(date +%s) - REVIEW_START ))
  TOKEN_COUNT=$(jq -r '.totals.tokens' "$STATE_FILE")

  completed_batches=$(jq -r '[.batches[] | select(.status == "completed")] | length' "$STATE_FILE")
  failed_batches=$(jq -r '[.batches[] | select(.status == "failed_transient")] | length' "$STATE_FILE")
  total_batches=$(jq -r '.batches | length' "$STATE_FILE")
  inline_comments_posted=$(jq -r '.totals.inline_comments_posted' "$STATE_FILE")

  REVIEW_OUTPUT=$(jq -r '
    . as $root
    | [
        "**Critical**",
        (if ([(.findings // {}) | to_entries[]? | select(.value.severity == "critical")] | length) == 0 then "- None." else ([(.findings // {}) | to_entries[]? | select(.value.severity == "critical") | "- \(.value.path):\(.value.line) - \(.value.message)"] | join("\n")) end),
        "",
        "**High**",
        (if ([(.findings // {}) | to_entries[]? | select(.value.severity == "high")] | length) == 0 then "- None." else ([(.findings // {}) | to_entries[]? | select(.value.severity == "high") | "- \(.value.path):\(.value.line) - \(.value.message)"] | join("\n")) end),
        "",
        "**Medium**",
        (if ([(.findings // {}) | to_entries[]? | select(.value.severity == "medium")] | length) == 0 then "- None." else ([(.findings // {}) | to_entries[]? | select(.value.severity == "medium") | "- \(.value.path):\(.value.line) - \(.value.message)"] | join("\n")) end),
        "",
        "**Low**",
        (if ([(.findings // {}) | to_entries[]? | select(.value.severity == "low")] | length) == 0 then "- None." else ([(.findings // {}) | to_entries[]? | select(.value.severity == "low") | "- \(.value.path):\(.value.line) - \(.value.message)"] | join("\n")) end),
        "",
        "Merge recommendation: " + (if ([(.findings // {}) | to_entries[]? | select(.value.severity == "critical" or .value.severity == "high")] | length) > 0 or ([.batches[] | select(.status != "completed")] | length) > 0 then "Block" else "Pass" end),
        "",
        "Batch progress: completed=" + (([.batches[] | select(.status == "completed")] | length) | tostring) + "/" + ((.batches | length) | tostring) + ", failed=" + (([.batches[] | select(.status == "failed_transient")] | length) | tostring)
      ]
    | join("\n")
  ' "$STATE_FILE")

  printf '%s\n' "$REVIEW_OUTPUT" >"$CODEX_LAST_MESSAGE_FILE"
  printf '%s\n' "$REVIEW_OUTPUT" >"$CODEX_STDOUT_FILE"
  echo "Completed in ${REVIEW_DURATION}s (tokens: ${TOKEN_COUNT}, completed batches: ${completed_batches}/${total_batches}, failed batches: ${failed_batches}, inline comments posted: ${inline_comments_posted})"

endgroup
fi

FOOTER="_Review completed in ${REVIEW_DURATION}s | tokens: ${TOKEN_COUNT}_"
BODY=$(printf '%s\n\n%s\n\n---\n%s' "$BRAD_BOT_TAG" "$REVIEW_OUTPUT" "$FOOTER")

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    printf '## Brad AI Review\n\n'
    printf '%s\n' "$BODY"
    printf '\n'
  } >>"$GITHUB_STEP_SUMMARY"
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  REVIEW_OUTPUT_FOR_GHA="$BODY"
  if [[ ${#REVIEW_OUTPUT_FOR_GHA} -gt 60000 ]]; then
    REVIEW_OUTPUT_FOR_GHA="${REVIEW_OUTPUT_FOR_GHA:0:60000}"
  fi
  {
    echo "brad_review_markdown<<__BRAD_EOF__"
    printf '%s\n' "$REVIEW_OUTPUT_FOR_GHA"
    echo "__BRAD_EOF__"
  } >>"$GITHUB_OUTPUT"
fi

if [[ "$DRY_RUN" == true ]]; then
  echo ""
  echo "============ DRY RUN OUTPUT ============"
  echo "$BODY"
  echo "========================================"
else
  group "Post review"
  REVIEW_SUMMARY_BODY="$BODY"
  gh_post "$GITHUB_API/pulls/$PR_NUMBER/reviews" \
    -d "$(jq -n --arg b "$REVIEW_SUMMARY_BODY" '{body:$b, event:"COMMENT"}')" >/dev/null
  echo "Posted review to PR #$PR_NUMBER"
  endgroup
fi

HAS_CRITICAL=$(echo "$REVIEW_OUTPUT" | awk '/^\*\*Critical\*\*/{getline; if ($0 !~ /^- None\./) print "found"}' || true)
HAS_HIGH=$(echo "$REVIEW_OUTPUT" | awk '/^\*\*High\*\*/{getline; if ($0 !~ /^- None\./) print "found"}' || true)
HAS_FAILED_BATCHES=""
if [[ -f "$STATE_FILE" ]]; then
  failed_batch_count=$(jq -r '[.batches[] | select(.status != "completed")] | length' "$STATE_FILE")
  if [[ "$failed_batch_count" -gt 0 ]]; then
    HAS_FAILED_BATCHES="found"
  fi
fi

if [[ -n "$HAS_CRITICAL" ]] || [[ -n "$HAS_HIGH" ]] || [[ -n "$HAS_FAILED_BATCHES" ]]; then
  echo ""
  echo "⚠️  Blocking conditions detected!"
  if [[ -n "$HAS_CRITICAL" ]]; then
    echo "   - Critical issues found"
  fi
  if [[ -n "$HAS_HIGH" ]]; then
    echo "   - High severity issues found"
  fi
  if [[ -n "$HAS_FAILED_BATCHES" ]]; then
    echo "   - One or more batches failed or remain incomplete"
  fi
  echo "Done (${REVIEW_DURATION}s) - FAILED"
  exit 1
fi

echo "Done (${REVIEW_DURATION}s)"
