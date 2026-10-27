# Engineering workspace validation

Verified on macOS arm64, Python 3.12, 28 October 2026 (Asia/Shanghai).

## Real coding task

A disposable Git project contained an unimplemented `parse_duration(text)` and six fixed unittest cases: compound and adjacent units, zero, whitespace, unit order, and malformed/repeated units. Codex ran inside a real Engineering PTY, using `zhunt.launch.build_plan` with a local Zhunt endpoint and the `zhunt-auto` alias. It inspected files, wrote the implementation through its shell tool, and ran the tests. All six passed; the test file SHA-256 remained `1e781dcc09ad6aa01de47d97456b19d51b976cc15614c3936de3b0d4a974ff98`.

A follow-up in the same Codex session added two tests (including four invalid whitespace subcases). The agent edited the implementation and executed all eight tests successfully. An independent test run confirmed the result. The session was subsequently resumed with `zhunt-coding` to verify the existing explicit tier override and corrected usage reporting.

The test used an isolated Zhunt home, no provider keys, paid routing disabled and training providers disabled. No paid model request or synthetic provider response was used. An isolated router on a second localhost port captured synthetic-project traffic for diagnosis; the terminal itself stayed in the Engineering workspace. Raw traces, credentials and local machine configuration are not included in Git.

## Live switching evidence

The initial successful implementation traversed these real provider attempts:

| Request | Attempt | Model | Result |
| --- | --- | --- | --- |
| Write implementation | 1 | OVH Qwen3.8-27B | No usable stream events |
| Write implementation | 2 | OVH Qwen3.5-397B-A17B | No usable stream events |
| Write implementation | 3 | OVH gpt-oss-120b | Executable file-writing tool call |
| Run tests | 1 | OVH Qwen3.6-27B | No usable stream events |
| Run tests | 2 | OVH gpt-oss-20b | Executable test command; six tests passed |
| Final response | 1 | OVH gpt-oss-20b | Task summary |

Failover was natural; no 429 was injected. The follow-up also recovered from unusable provider responses and retained the coding conversation. After the usage fix, a live `zhunt-coding` response recorded **15,552 input tokens and 127 output tokens**, matching the provider stream's usage report.

## Fixes found by live testing

- Empty provider streams now fail instead of appearing as successful empty answers, allowing the existing retry engine to switch models.
- Responses streaming requests ask for usage totals; completed/incomplete event usage is read by the quota and telemetry code.
- Known channel markers appended to an exact advertised tool name are removed. Unknown tools and unknown suffixes are not rewritten; tool permissions are unchanged.
- Responses custom tools are bridged through a JSON `input` string, including streamed calls and replayed results. This bridge is regression-tested; the installed Codex fallback configuration did not advertise a custom patch tool, so the live task used shell edits. Provider-side custom grammar constraints are not enforced by Chat Completions.

## Verification and limits

`python -m pytest -ra`: **727 passed**, one existing Starlette/httpx deprecation warning.

Tests cover real PTY I/O and resize, task working directories, project persistence, isolated Git worktrees, authentication, launch plans, secret environment filtering, and child termination on daemon shutdown. Browser checks covered floating/tiled layouts, terminal input, and eight passing coding tests; no browser console errors were observed.

The keyless free pool is slow and variable. Early attempts produced invalid argument types, invented unavailable tools, or empty streams. The successful run required retries and prompt clarification. This is a verified integration, not a guarantee that every free model can complete every coding task. Native Claude Code/Codex account launches remain available, but paid inference was not exercised. Integrated PTYs currently support macOS/Linux; Windows is not implemented. Terminal output and processes do not survive a daemon restart; project/task definitions do.
