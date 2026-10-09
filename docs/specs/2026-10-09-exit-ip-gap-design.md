# Exit IP reuse with a minimum gap

## Problem

`UNIQUE_EXIT_IPS` (3.1) never lets one survey submit twice from the same Tor
exit IP. On a 430-response survey through six SocksPorts the pool of fresh
exits runs dry: at 150 submissions the rule already cost ~1.2 rejected
circuits per success, and the ratio only grows. What the client actually
needs is weaker: the same IP may appear more than once, just never in
successive submissions.

## Design

One setting, `EXIT_IP_MIN_GAP` (integer, default `0`):

- `0` — today's rule: an IP in the ledger is never used again.
- `N > 0` — an IP may be reused once at least `N` other submissions of this
  survey were recorded after its last use. `N=1` is exactly "never twice in a
  row"; a larger `N` spreads repeats further apart.

The ledger (`output/<id>/exit_ips.jsonl`) already stores submissions in order,
so it now keeps the position of each IP's last use. An IP that is reserved by
an in-flight session is still refused, whatever the gap, so concurrent
sessions never share an exit. The rule applies only while `UNIQUE_EXIT_IPS` is
on; with it off nothing is checked, as before.

## Not changed

- The ledger format (old ledgers load unchanged).
- When the check runs: before the form loads, as a `CircuitError`, so a
  refused IP never posts anything.
- Isolation between surveys: each survey has its own ledger.
