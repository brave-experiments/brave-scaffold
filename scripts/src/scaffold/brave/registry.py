# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Public command definitions. Help, parsing, and dispatch all read this table."""

from __future__ import annotations

from ..common.cli import CommandSpec, Opt, Positional
from . import android, clean, cmd_build, cmd_patches, cmd_setup, cmd_tools, doctor

WITH_PYTHONPATH = Opt("--with-pythonpath", "with_pythonpath", takes_value=False,
                      help="Also export PYTHONPATH for Core's script directory.")
CWD = Opt("--cwd", "cwd", metavar="DIRECTORY",
          help="Run in this directory (relative to your current directory) instead of the current one.")

CONFIGURATION = Opt("--configuration", "configuration", choices=("debug", "release"),
                    help="Build configuration (default: debug).", metavar="CONFIG")
OFFLINE = Opt("--offline", "offline", takes_value=False,
              help="Compile locally instead of using remote build execution (RBE/Siso, the default).")
FORCE_GN = Opt("--force-gn", "force_gn", takes_value=False, help="Regenerate GN files even if they exist.")
PLAN = Opt("--plan", "plan", takes_value=False, help="Show the steps without running any that change anything.")
SKIP_SUPPORT_REFRESH = Opt("--skip-support-refresh", "skip_support_refresh", takes_value=False,
                           help="Stop an Android build if support patches or resources need refreshing.")
ARTIFACT = Opt("--artifact", "artifact", metavar="PATH", help="Application to run instead of the default output.")
FILTER = Opt("--filter", "filter", metavar="PATTERN", help="Only run tests matching the pattern within the suite.")
DEVICE = Opt("--device", "device", metavar="ID", help="Android device id (required when several are usable).")
SOURCE = Opt("--source", "source", metavar="URL_OR_PATH", help="Support repository to clone (default: the standard source).")
REF = Opt("--ref", "ref", metavar="REF", help="Support repository branch, tag, or commit for this checkout only.")
ADOPT = Opt("--adopt-local-changes", "adopt_local_changes", takes_value=False,
            help="Disabled: blanket adoption cannot establish which local changes may be discarded.")
OVERWRITE = Opt("--overwrite-local-changes", "overwrite_local_changes", takes_value=False,
                help="Back up and overwrite the listed local file changes for this sync only.")
DIFF = Opt("--diff", "diff", takes_value=False, help="Print the Git diff of each drifted file.")
TARGET = Positional("target", help="mac or android (default: configured platform, else this host).")
BUILD_SIDE_EFFECTS = ("Writes the build output under the checkout's src/out, applies Core patches when they are "
                      "out of date and no local edits are at risk, and may update the Metal toolchain setting for the "
                      "child only. Android support refresh may reset patch targets and replace or sign resources in "
                      "its declared paths. Never cleans, installs, or launches anything.")

# Commands that take a group word first ("checkout add"). The value is the set of subcommands.
GROUPS = {"checkout": ("add", "list"), "env": ("init", "export", "check"), "tools": ("setup",),
          "patches": ("update",), "android": ("setup",)}


def build_registry():
    specs = [
        CommandSpec("context", "Show the resolved checkout, environment mapping, configuration, and tool state.",
                    cmd_setup.context, examples=("bdev context", "bdev context --checkout main --json")),
        CommandSpec("capabilities", "List supported, limited, unverified, and unsupported combinations.",
                    cmd_setup.capabilities, examples=("bdev capabilities --json",)),
        CommandSpec("doctor", "Check readiness for a scope without repairing anything.", doctor.run_doctor,
                    positionals=(Positional("scope", help="mac, android, rbe, shell, or signing; omit for all scopes."),),
                    examples=("bdev doctor", "bdev doctor mac --checkout main"),
                    notes="Checking a selected checkout evaluates its approved environment file. Refresh warnings do not guarantee a build can repair the files."),
        CommandSpec("setup", "Inspect prerequisites and prepare scaffold-owned configuration.", cmd_setup.setup, creates_config=True,
                    side_effects="Creates brave-scaffold.toml beside the scaffold when it is missing. "
                                 "Nothing inside Brave Core changes.",
                    examples=("bdev setup",)),
        CommandSpec("checkout add", "Register an existing checkout under a name.", cmd_setup.checkout_add, creates_config=True,
                    positionals=(Positional("name", True, help="Alias for the checkout."),
                                 Positional("path", True, help="Core, Chromium src, or outer checkout path.")),
                    side_effects="Edits brave-scaffold.toml. Nothing inside the checkout changes.",
                    examples=("bdev checkout add main /work/browser/_bad_scm/workspace/src/brave",)),
        CommandSpec("checkout list", "List registered checkouts, environment state, and invalid registrations.",
                    cmd_setup.checkout_list, examples=("bdev checkout list",)),
        CommandSpec("env init", "Generate the scaffold-owned environment for a checkout and print the approval command.",
                    cmd_setup.env_init,
                    side_effects="Writes <environment-dir>/.envrc and the checkout record in brave-scaffold.toml. "
                                 "Never approves the file and never writes inside Brave Core.",
                    examples=("bdev env init --checkout main",)),
        CommandSpec("env export", "Print derived checkout exports for a generated .envrc (no direnv, no tool changes).",
                    cmd_setup.env_export, options=(WITH_PYTHONPATH,),
                    examples=("bdev env export --checkout main --format bash",)),
        CommandSpec("env check", "Compare the loaded environment against the checkout identity and tools.",
                    cmd_setup.env_check, options=(WITH_PYTHONPATH,),
                    notes="Evaluates the approved environment file, which is user-reviewed code.",
                    examples=("bdev env check --checkout main",)),
        CommandSpec("shell", "Start a child shell in Core using the approved environment.", cmd_setup.shell,
                    notes="Exiting the shell leaves your original environment unchanged.",
                    examples=("bdev shell --checkout main",)),
        CommandSpec("tools setup", "Explicitly provision or repair checkout-local Node and package-manager payloads.",
                    cmd_tools.tools_setup,
                    side_effects="Downloads and deploys the checkout's pinned payloads inside the checkout's "
                                 "third_party/node directory using the checkout's own installer.",
                    examples=("bdev tools setup --checkout main",)),
        CommandSpec("vpython3", "Run the checkout-local vpython3 with your arguments.", cmd_tools.vpython3,
                    options=(CWD,), forward=True, leading_only=True,
                    side_effects="Whatever the Python program does. Runs in your current directory unless --cwd is given.",
                    notes="Scaffold options must come before the first Python argument. Use -- to be explicit.",
                    examples=("bdev vpython3 -- tools/example.py --flag", "bdev vpython3 --cwd out -- ../script.py")),
        clean.SPEC,
        CommandSpec("build", "Prepare and compile Brave for a target, then verify its output.", cmd_build.cmd_build,
                    positionals=(TARGET,), options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, SKIP_SUPPORT_REFRESH), forward=True,
                    side_effects=BUILD_SIDE_EFFECTS,
                    notes="Unknown options and extra arguments go to 'bpm run build' after the generated ones.",
                    examples=("bdev build", "bdev build mac --offline", "bdev build --plan")),
        CommandSpec("build-run", "Build, then restart the browser with exactly the output that build produced.",
                    cmd_build.cmd_build_run, aliases=("br",), positionals=(TARGET,),
                    options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, DEVICE, SKIP_SUPPORT_REFRESH), forward=True,
                    side_effects=BUILD_SIDE_EFFECTS + " Then stops any running instance of the same application and "
                                                     "launches the new build.",
                    examples=("bdev br",)),
        CommandSpec("sync", "Run the supported Core source sync.", cmd_build.cmd_sync,
                    positionals=(Positional("targets", help="Comma-separated targets: mac, android."),),
                    options=(PLAN, ADOPT, OVERWRITE), forward=True,
                    side_effects="Updates the checkout's sources and dependencies. Stops first if local work "
                                 "could be overwritten; interactive use offers backup and overwrite approval. "
                                 "Mobile targets keep the checkout's existing target_os values.",
                    examples=("bdev sync", "bdev sync mac,android --plan")),
        CommandSpec("sync-build", "Sync, then build; stops at the first failed phase.", cmd_build.cmd_sync_build,
                    aliases=("sb",), positionals=(TARGET,),
                    options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, ADOPT, OVERWRITE, SKIP_SUPPORT_REFRESH),
                    forward=True, side_effects="Sync effects, then build effects.", examples=("bdev sb",),
                    notes="Extra arguments go to the build phase only."),
        CommandSpec("sync-build-run", "Sync, build, then restart the browser with the built output.",
                    cmd_build.cmd_sync_build_run, aliases=("sbr",), positionals=(TARGET,),
                    options=(CONFIGURATION, OFFLINE, FORCE_GN, PLAN, DEVICE, ADOPT, OVERWRITE, SKIP_SUPPORT_REFRESH), forward=True,
                    side_effects="Sync, build, and restart effects.", examples=("bdev sbr",),
                    notes="Extra arguments go to the build phase only."),
        CommandSpec("test", "Compile if needed and run one test suite (macOS, or Android JUnit and device tests).",
                    cmd_build.cmd_test,
                    positionals=(TARGET, Positional("suite", True, help="Test suite, for example brave_unit_tests; on "
                                                    "Android brave_junit_tests or brave_java_unit_tests.")),
                    options=(CONFIGURATION, OFFLINE, FILTER, PLAN, DEVICE), forward=True, max_positionals=2,
                    post_parse=cmd_build.post_parse_test,
                    side_effects=BUILD_SIDE_EFFECTS.replace("Never cleans, installs, or launches anything.",
                                                            "Runs the tests, which may launch test browsers.")
                    + " Android: also requires the support working copy on the android-testing-prototype branch "
                      "(never switched), applies the support repository's test overlay to Core's build/commands "
                      "and leaves it applied, builds in out/android_tests_<configuration>_arm64, and "
                      "brave_java_unit_tests runs on the selected device.",
                    notes="The suite must come before any forwarded arguments. --filter only narrows the suite. "
                          "On Android, brave_junit_tests runs on this Mac and takes no --device; "
                          "brave_java_unit_tests needs a device (--device is a scaffold option, not forwarded). "
                          "Host-side filters need a fully qualified class or a wildcard such as '*ExampleTest*'.",
                    examples=("bdev test brave_unit_tests", "bdev test mac brave_browser_tests --filter 'Example.*'",
                              "bdev test android brave_junit_tests --filter='*BraveCommandLineInitUtilTest*'",
                              "bdev test android brave_java_unit_tests --filter='BraveAppearancePreferencesTest.*' "
                              "--device=emulator-5554")),
        CommandSpec("run", "Restart the browser with an existing output; never builds.", cmd_build.cmd_run,
                    positionals=(TARGET,), options=(CONFIGURATION, ARTIFACT, PLAN, DEVICE),
                    side_effects="macOS: quits any running instance of the same application (from any checkout), "
                                 "then launches the selected one. Android: installs the APK over the existing app on "
                                 "one device, stops that package there, and launches it. Profiles and app data are kept.",
                    notes="Older or independently built outputs may run; source state is not inspected.",
                    examples=("bdev run", "bdev run --artifact ./out/Custom/'Brave Browser Development.app'")),
        CommandSpec("deploy", "Install the Android build on a device and launch it (same as 'run android').",
                    cmd_build.cmd_deploy, positionals=(Positional("target", True, help="android"),),
                    options=(CONFIGURATION, ARTIFACT, PLAN, DEVICE),
                    side_effects="Installs the APK over the existing app on one device (data is kept), stops that "
                                 "package there, and launches it.",
                    examples=("bdev deploy android", "bdev deploy android --device emulator-5554")),
        CommandSpec("android setup", "Create this checkout's Android-on-Mac support working copy.",
                    android.cmd_android_setup, options=(SOURCE, REF),
                    side_effects="Uses the network: updates a shared Git object cache under .bdev/cache and clones "
                                 "the support repository into <workspace>/brave-android-mac-support for this "
                                 "checkout. An existing working copy is switched only to an explicit --ref, and only "
                                 "when it has no local changes and no unpushed commits.",
                    examples=("bdev android setup --checkout main", "bdev android setup --ref main")),
        CommandSpec("drift", "Compare patched Chromium files with the patch metadata (read-only).", cmd_patches.cmd_drift,
                    options=(DIFF,), examples=("bdev drift", "bdev drift --diff")),
        CommandSpec("patches update", "Generate Core patch changes from local Chromium edits.",
                    cmd_patches.cmd_patches_update, forward=True,
                    side_effects="Runs 'bpm run update_patches', which rewrites patch files in Core. Nothing is "
                                 "committed.", examples=("bdev patches update",)),
    ]
    registry = {}
    for spec in specs:
        registry[spec.name] = spec
        for alias in spec.aliases:
            registry[alias] = spec
    return registry


REGISTRY = build_registry()
