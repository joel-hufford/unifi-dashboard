Scratch space for diagnostic output from the running application.

`*.json` here is gitignored on purpose: a controller dump carries real MAC
addresses, public IPs, site names and cell IDs. Keep them local.

Useful captures:

    curl -s http://127.0.0.1:8787/api/debug/wan | python3 -m json.tool > diagnostics/wan.json
    curl -s http://127.0.0.1:8787/api/dashboard | python3 -m json.tool > diagnostics/dashboard.json

A scrubbed copy of a real UCG-Max payload lives in
`tests/fixtures/ucg_max_dual_wan.json` and is what the WAN detection tests
run against.
