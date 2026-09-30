# macOS

## Readiness and RBE/Siso prerequisites

`bdev doctor mac` and `bdev doctor rbe` report these checks without changing
anything. Nothing is installed, repaired, synced, or approved, and nothing is
written to the checkout.

### macOS build readiness (`doctor mac`)

| Check | Verifies | Required |
| --- | --- | --- |
| `macos-sdk` | `xcrun --show-sdk-version` reports an SDK | yes |
| `metal-toolchain` | `xcrun metal` works, or a Metal toolchain component is mounted | no (warning) |
| `disk-space` | At least 150 GiB free where the checkout lives | no (warning) |
| `services-key` | `brave_services_key` is nonempty in Core's `.env` (including `include_env=` files). The value is never shown and its validity is not verified | no (warning) |
| `rbe-*` | The RBE checks below, as warnings only | no |

The Xcode developer directory is checked separately by `host-macos-arm64` and
`xcode-developer-directory`. Node, the package manager, and `vpython3` are
checked by `local-tools`, from the checkout's own payloads.

Checks that need a checkout report `not_checked` when none is selected; the
machine checks still run.

### RBE/Siso configuration (`doctor rbe`)

RBE is the default for desktop compilation. These checks read local files only.

| Check | Verifies |
| --- | --- |
| `rbe-env` | `.env` sets `rbe_service`, `rbe_tls_client_auth_cert`, `rbe_tls_client_auth_key`, `siso_cache_dir`, and `use_remoteexec=true` |
| `rbe-siso-mode` | Native Siso mode: `use_siso` is true (default) and `use_reclient` is false (default) |
| `rbe-tls-files` | The TLS certificate and key are readable. Contents are never read or shown |
| `rbe-tls-expiry` | The certificate is valid for at least 24 hours (needs `openssl`) |
| `rbe-siso-cache` | The Siso cache directory exists |
| `rbe-gclient`, `rbe-sisorc`, `rbe-sisoenv` | Sync outputs (`.gclient`, `.sisorc`, `.sisoenv`) reference the RBE service and cache. Stale outputs need a sync with the internal VPN connected |
| `rbe-gn-outputs` | Existing `out/*/args_generated.gni` files agree with RBE (warning only; a forced GN regeneration at build time fixes it) |
| `rbe-reachability` | Always `not_checked` |

In scope `rbe`, problems with these are required blockers. In scope `mac` they
are warnings, because compiling locally with `--offline` needs no RBE. Use
`--offline` on the build command to choose local compilation explicitly; the
tools never fall back to it silently.

### Network limits

Doctor does not test the internal VPN, the RBE service, or authentication with
it. A passing result means the local configuration looks complete, not that a
remote build will succeed. Confirm the VPN yourself before an RBE build or sync.
Service addresses that contain credentials are redacted in results.
