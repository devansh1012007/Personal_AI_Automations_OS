#!/bin/sh
#
# examples/shell-pipeline/pipeline.sh — the P3.4 language proof.
#
# paa-pipeline/1 spoken by POSIX shell, with NO SDK and NO imports: the
# protocol section of docs/plans/2026-11-21-pipeline-platform-spec.md (5)
# is this script's only reference. The wire is one compact JSON object per
# line on stdin, replies on stdout, free-form logging on stderr; the
# capability tokens for the granted set arrive out of band in the
# PAA_PIPELINE_TOKENS environment variable. The script completes the whole
# conversation the host drives: pipeline.initialize, pipeline.run (one
# always-served ctx.now call, then one token-carrying ctx.think call), the
# final result, pipeline.shutdown.
#
# HONEST BOUNDARY, stated in the first person: this script handles the
# protocol's FIXED SHAPES, not general JSON. Every extraction below — the
# envelope's request id, the recorded clock, the think token — assumes the
# exact byte shapes the frozen protocol defines for these frames (compact
# separators, the envelope's id first, the tokens env a flat one-level
# object of granted names to hex nonces). A reordered field, an escaped
# quote inside a value, or a shape the protocol never promised would be
# misread here. That is the point of the proof — the doc is implementable
# from alone — and the limit of the artifact: a real pipeline written in a
# language without a JSON library should wear the same warning.
#
# Driven by tests/pipelines/test_rpc_host.py through a real
# RpcPipelineHost over a real sandbox (skipped where no bash exists).

NAME=shell.demo
VERSION=0.1.0
PROTOCOL=paa-pipeline/1

# The envelope's request id: the integer after the first "id": on the
# line. Every host->child request carries the envelope before the params,
# and no field the protocol defines for these frames can match earlier.
req_id() {
    printf '%s' "$1" | awk 'match($0, /"id":[0-9]+/) {
        print substr($0, RSTART + 5, RLENGTH - 5); exit
    }'
}

# One granted capability's token, from the out-of-band env var the host
# set at launch: {"think":"<hex nonce>"} for exactly the granted names.
# An ungranted capability has no entry, so the call goes out with an
# empty token and the host refuses it -32006 — the honest outcome.
token_for() {
    printf '%s' "${PAA_PIPELINE_TOKENS-}" \
        | sed -n 's/.*"'$1'":"\([^"]*\)".*/\1/p'
}

# The recorded clock the host served for ctx.now: "result":"<iso-8601>".
served_now() {
    printf '%s' "$1" | sed -n 's/.*"result":"\([^"]*\)".*/\1/p'
}

echo "shell child: speaking $PROTOCOL from the protocol doc alone" >&2

while IFS= read -r line; do
    case $line in
    *'"method":"pipeline.initialize"'*)
        case $line in
        *'"protocol":"'$PROTOCOL'"'*) ;;
        *)
            printf '{"jsonrpc":"2.0","id":%s,"error":{"code":-32602,"message":"protocol mismatch: this pipeline speaks %s"}}\n' \
                "$(req_id "$line")" "$PROTOCOL"
            exit 0
            ;;
        esac
        printf '{"jsonrpc":"2.0","id":%s,"result":{"ok":true,"name":"%s","version":"%s"}}\n' \
            "$(req_id "$line")" "$NAME" "$VERSION"
        ;;
    *'"method":"pipeline.run"'*)
        # Seq 1, no token: the always-served determinism aid. The child
        # side owns its request ids; 0 and 1 are this run's two calls.
        printf '{"jsonrpc":"2.0","id":0,"method":"ctx.now","params":{"seq":1}}\n'
        now=""
        while IFS= read -r reply; do
            case $reply in
            *'"id":0,'*) now=$(served_now "$reply"); break ;;
            esac
        done
        # Seq 2, token-carrying: the grant check's half of the contract.
        printf '{"jsonrpc":"2.0","id":1,"method":"ctx.think","params":{"seq":2,"token":"%s","prompt":"what shall we do?","role":"worker"}}\n' \
            "$(token_for think)"
        thought=unanswered
        while IFS= read -r reply; do
            case $reply in
            *'"answer"'*) thought=yes; break ;;
            *'"error"'*) thought=refused; break ;;
            esac
        done
        printf '{"jsonrpc":"2.0","id":%s,"result":{"status":"completed","summary":"now=%s think=%s","metrics":{"ctx_calls":2},"artifacts":[{"kind":"shell-demo"}]}}\n' \
            "$(req_id "$line")" "$now" "$thought"
        ;;
    *'"method":"pipeline.cancel"'*)
        # The demo holds no long-running work to abort: answer and leave.
        # A real child answers its in-flight pipeline.run with -32005
        # before exiting — that behaviour is the SDK child's, pinned by
        # the cancel tests; this proof's runs are short by construction.
        printf '{"jsonrpc":"2.0","id":%s,"result":{"ok":true}}\n' \
            "$(req_id "$line")"
        exit 0
        ;;
    *'"method":"pipeline.shutdown"'*)
        printf '{"jsonrpc":"2.0","id":%s,"result":{"ok":true}}\n' \
            "$(req_id "$line")"
        exit 0
        ;;
    *'"method"'*)
        printf '{"jsonrpc":"2.0","id":%s,"error":{"code":-32601,"message":"unknown method"}}\n' \
            "$(req_id "$line")"
        ;;
    esac
done
exit 0
