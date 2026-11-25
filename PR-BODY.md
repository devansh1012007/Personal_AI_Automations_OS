## Attested change bundle

- **Bundle**: `d1412c20f13adacb7fce6e53` (tool 4.1.0, 
2026-11-25T03:27:28.565+00:00)
- **Goal**: ship the demo feature, honestly attested
- **Requested by** (asserted): `cli:operator` under policy `AUTO`
- **Chain root**: 
`8f06f99d47244c421d0b1597a7124c288d3fd4e3f68a70c1e7772f1f245a5d68`
- **Signed**: ed25519 (key `6827bc6d157fa48b…`)
- **Tests**: 1 observed (1 green) / 0 asserted
- **Approvals**: 1 recorded
- **Evidence bundles**: 1 (T1 observe bundles, content-addressed)

### How to verify

```bash
paa verify /tmp/paa-attest-demo-home-9ordjxu7/attest/20261125T032728 --trust 
<signer-public-key>
```

The command runs offline: it recomputes every blob's hash, re-walks
the chain and checks the signature against the embedded public key.
Without `--trust` it reports the key as *self-asserted* — integrity
checked, origin not claimed.
