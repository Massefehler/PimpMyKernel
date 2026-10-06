# PimpMyKernel

**English** · [Deutsch](README.de.md)

A desktop assistant for Debian/Ubuntu: select official Linux kernels from
kernel.org, build them using an existing `/boot/config-*`, export `.deb`
packages and optionally install them. The interface is available in **English,
German and Spanish**, with automatic detection and a saved manual choice.
PimpMyKernel itself is distributed as an installable `.deb` package.

## Install the application

Requires Ubuntu 24.04 or later, or a Debian system with **Python 3.12+**,
**Qt 6.4+** and a graphical desktop session.

Download the application package from
[GitHub Releases](https://github.com/Massefehler/PimpMyKernel/releases/latest)
and run this command in the download directory:

```bash
sudo apt install ./pimpmykernel_0.1.8_all.deb
```

Open **PimpMyKernel** from the application menu or run `pimpmykernel`.
Start the interface as your regular user. A graphical administrator prompt
appears when an action requires elevated privileges.

To build the application package from source:

```bash
./build-deb.sh
sudo apt install ./dist/pimpmykernel_0.1.8_all.deb
```

The packaging script uses Python and `dpkg-deb`, requires no root privileges
and writes to `dist/`. Use `./build-deb.sh --output /path/to/directory` to
choose another output directory. Set `SOURCE_DATE_EPOCH` for reproducible
package metadata. The original application icon is `icon/PimpMyKernel.png`.

## System language and manual language selection

At startup, the application reads the desktop's preferred interface languages
through Qt and chooses the first supported language: **German**, **English**
or **Spanish**. Regional variants such as `de_AT`, `de_CH`, `en_GB`, `en_US`,
`es_ES`, `es_MX` and `es_AR` are supported. English is used if no supported
language is found.

Under **System check → Language**, choose **Automatic (system language)**,
**Deutsch**, **English** or **Español**. A manual choice overrides the system
language, so an English interface can be used on a Spanish system, for example.
The choice is saved immediately and takes effect on the **next application
start**. Running builds continue in their current language. Select
**Automatic** to follow the desktop language again after restarting the app.
Invalid or unsupported saved values fall back to automatic detection.

Status messages, tooltips, help, errors and the dialogs for kernel installation,
removal and menuconfig are translated. Numbers and dates follow the system
format. Native Qt buttons and file dialogs use `qt6-translations-l10n`, which
is installed as a package dependency. External make, APT and GPG output stays
in the C locale for reliable parsing; application messages are translated.
Administrator and terminal helpers explicitly receive the application language,
even when their execution environment is cleaned.

Translation catalogs are separate from the application code:
`pimpmykernel/assets/i18n/en.json` and `es.json` map German source messages to
English and Spanish translations. To add another language, create a catalog
with the same structure, register it in `SUPPORTED_LANGUAGES` in
`pimpmykernel/i18n.py` and add its native name to the language selector.
Keep named placeholders such as `{version}` unchanged. Tests check completeness
and placeholders. Missing translations fall back to English. Application
changes are always delivered with a newly built `.deb` package.

## Workflow

1. **Select a kernel:** The current release list is fetched live from
   [releases.json](https://www.kernel.org/releases.json). Stable is selected
   by default; maintained LTS releases are also available. RCs can be displayed,
   but cannot be built without an official release signature. **Load older
   versions** adds the official archive for all kernel series from Linux 4.x
   onward, including earlier releases such as **7.2.8** and **7.2.3**.
   Combine the **Kernel series** and **Version search** filters, for example
   Linux 6.x and `6.1.`. Versions are sorted in descending order, with the
   current stable release selected by default. Current and archived releases
   are merged without duplicates. Refreshing the release list preserves loaded
   archive versions and the selection. Only source archives with a matching
   release signature are offered for building. If the system is already running
   the latest stable kernel, a notice is displayed. The comparison accounts
   for suffixes such as `-pmk` and distribution ABI additions.
2. **Prepare the build:** The newest `/boot/config-*` by version is selected
   by default. Another existing configuration can be chosen. The running
   kernel's configuration is marked in the list.
3. **Choose parallel jobs:** Set separate job counts for `make` and
   `make bindeb-pkg`. Available values include `-j8`, `-j10`, `-j12` and `-j16`
   when the process's CPU affinity allows enough threads, along with smaller
   values.
4. **Choose an export directory and start:** The assistant checks tools and
   free space, downloads the official `.tar.xz` and `.tar.sign`, extracts the
   archive and verifies its OpenPGP signature **before** executing anything
   from the source tree. Only trusted release publishers are accepted. A
   download bar shows the percentage and transferred MiB. It reaches **100%**
   only after the download completes successfully, and remains visible during
   later build stages. If the total size is unknown, the transferred amount
   is shown initially.
5. **Configure:** The selected file is copied to `.config`. `make oldconfig`
   receives empty answers to accept kernel defaults for new options. The visible
   build options are then applied and `make olddefconfig` resolves dependencies.
   Experienced users can optionally run `make menuconfig` and review their
   changes before compilation.
6. **Build:** Run `make -jN LOCALVERSION=-pmk`, followed by
   `make bindeb-pkg -jM LOCALVERSION=-pmk KDEB_PKGVERSION=<version>-1`.
7. **Export:** All generated `.deb` files are copied into a new, uniquely named
   subdirectory. Existing packages are never overwritten. The export also
   contains `kernel.config`, `original.config`, `config-changes.diff`,
   `manifest.json` with SHA-256 checksums and the complete `build.log`.
8. **Clean up:** When enabled, the archive and only the application's own
   marked build job are deleted, after the export has been verified.
9. **Install:** Review the package list and choose **Install kernel**. APT
   installs the local packages through `pkexec`. Previous kernels remain
   installed. The application does not restart the system automatically.
   **Exported kernel `.deb` files remain in the chosen export directory after
   installation.** Cleanup applies only to the build working directory and
   preserves exports and saved logs.

## Optional manual kernel configuration

Under **Prepare build → For Linux professionals and enthusiasts**, enable
**Open make menuconfig before compiling**. This option is disabled at each
application start. The interface explains that users should understand the
options they change: an incorrect configuration can remove drivers or prevent
the kernel from booting.

After downloading, verifying the signature and preparing the existing
configuration, `make menuconfig` opens in a separate **xterm** terminal.
The application package recommends xterm, so a normal APT installation installs
it as well. If it is missing, the application reports this before starting the
build; install it with `sudo apt install xterm`. The configuration libraries
are included in the offered build dependencies.

Make the desired changes, save them to **`.config`** and exit menuconfig.
PimpMyKernel displays the saved changes against the prepared configuration.
Compilation starts only after choosing **Continue build**. **Cancel build**
stops the operation and then offers cleanup. Closing the terminal early or an
error does not automatically resume compilation.

Build options applied earlier can be changed in menuconfig; manual settings
take precedence. `make olddefconfig` then resolves their dependencies.
The package suffix **`-pmk`** is retained. All build commands run as the regular
user; the original configuration is only read. The export additionally contains
**`before-menuconfig.config`** and **`menuconfig-changes.diff`**.
`kernel.config` records the final configuration, while `config-changes.diff`
still shows all changes relative to the original file.

## Manage installed kernels

The **Installed kernels** page reads installed packages from the Debian package
database. Each kernel shows its version, associated packages and installed size.
Select older kernels using the checkboxes, then choose **Uninstall selected
kernels** to remove them.

The running kernel has a visible **“This kernel is currently running and cannot
be removed.”** note and cannot be selected for removal. Hovering over the entry
explains this protection and lists its packages. The last installed kernel is
also protected if the running kernel is absent from the package database.
Associated headers, modules, debug packages and version-specific tools are
removed with a selected kernel. Shared headers and tools required by other
installed kernels are retained; general packages such as `linux-libc-dev`
are never removed.

Before confirmation, an APT simulation checks the exact package list. If APT
would remove additional packages, such as meta-packages for automatic
distribution kernel updates, the operation is stopped with an explanation.
The confirmation dialog shows the complete package list. Removal proceeds
only after explicit confirmation and graphical administrator authorization.
The privileged helper rechecks the running kernel, package list and APT removal
plan. No blanket `autoremove` is performed.

The list is refreshed afterward. Exported `.deb` files and build logs remain
available after uninstallation.

## Build options and requirements

- **Omit debug symbols/BTF** is enabled by default to reduce build time,
  storage use and package size. Disable it for BTF-based eBPF/tracing.
- **Automatically clear missing certificate paths · Ubuntu/Canonical** is
  enabled by default. Missing entries such as `debian/canonical-certs.pem` and
  `debian/canonical-revoked-certs.pem` in `CONFIG_SYSTEM_TRUSTED_KEYS` and
  `CONFIG_SYSTEM_REVOCATION_KEYS` are cleared in the copied configuration.
  Simply removing the word `canonical` from a filename would still leave a
  reference to a missing file. The normal module key `certs/signing_key.pem`
  remains in place and is generated by kbuild. Existing certificate files are
  preserved; a missing external module key is replaced by the kbuild key.
  Only the copied `.config` changes; the original `/boot/config-*` is preserved.
  The log records the old and new paths.
- The **`-pmk`** suffix distinguishes the custom kernel from the existing one.
- **System check** offers installation of build dependencies through APT.
  They are needed only when building a kernel.
- Plan for at least **30 GiB** of free space, or **60 GiB or more** with debug
  symbols. The amount depends on the inherited configuration. The preflight
  check blocks builds below 10 GiB, or below 25 GiB with debug symbols.
- Custom kernels are not automatically signed for **Secure Boot**. When
  Secure Boot is detected as enabled, integrated installation is disabled;
  exporting packages for a separate signing workflow remains possible. If
  its status cannot be determined, installation asks for explicit confirmation.
- Distribution and third-party drivers such as NVIDIA/DKMS may require
  additional steps. Reusing an existing configuration does not guarantee
  that a new kernel will boot on every device.

## Errors and cancellation

Downloads, the build process group and the menuconfig terminal can be cancelled.
After clicking **Cancel build**, running processes are stopped first. The
application then offers **Delete working files** or **Keep for troubleshooting**.
Deletion removes the download, source tree, compilation files, packages not yet
exported and temporary helper files from the application's own marked job.
Saved build logs and exported packages are retained. Cleanup runs in the
background so the interface remains responsive.

After a build error, or when files are kept, the working directory remains
available for diagnosis. It can be opened and removed later using **Delete
working directory**. Closing the window during a build stops the processes
and retains the working files. APT operations are allowed to finish.
The window waits for active workers before closing.

Working files: `${XDG_CACHE_HOME:-~/.cache}/pimpmykernel/jobs/`.
Build commands use a private `tmp/` directory inside their job as `TMPDIR`,
`TMP` and `TEMP`. These files are included in job cleanup; the general `/tmp`
directory is not cleared. Complete build logs:
`${XDG_STATE_HOME:-~/.local/state}/pimpmykernel/logs/`.
Settings are stored through Qt in the user configuration directory.
Each application start fetches releases from kernel.org. There are no hardcoded
current release versions or automatic installations.

## Run from source

```bash
sudo apt install git python3-pyqt6 libqt6svg6 qt6-translations-l10n pkexec ca-certificates gnupg xterm
git clone https://github.com/Massefehler/PimpMyKernel.git
cd PimpMyKernel
./start.sh
```

Alternatively, use a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
./start.sh
```

`python3 install-desktop.py` creates a local application-menu entry for a source
checkout. This is unnecessary when installing the `.deb` package.

## Development and checks

```bash
./start.sh --check
./start.sh --list-releases
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
./build-deb.sh
dpkg-deb --info dist/pimpmykernel_0.1.8_all.deb
```

Tests cover the complete workflow with a local test kernel and small real
Debian test packages, signature failures, export checksums, path protection,
process cancellation, download percentages and Qt interactions. Archive tests
cover signed file pairs, version sorting, all kernel series, combined filters,
cancellation and retries after loading errors. Kernel management uses local
package data and simulated APT plans, including protection for the running and
last kernel and shared headers. Tests do not install or uninstall kernels.
Menuconfig tests exercise a real terminal channel with keyboard input, errors,
early terminal closure, cancellation including child processes, review before
compilation and export of the manually selected configuration.
Language tests check complete German, English and Spanish messages, unchanged
placeholders, automatic detection and saved choices after restarting. They
also cover translated kernel-protection messages, standard dialogs and
terminal notices.

The optional `python3 tests/live_prepare.py --config /boot/config-<version>`
check downloads a real kernel, verifies its release signature and applies the
chosen configuration, including certificate-path cleanup. It does not compile
or install a kernel.

Official references:
[kernel signatures](https://www.kernel.org/signature.html),
[kernel configuration](https://docs.kernel.org/kbuild/kconfig.html),
[kernel sources and build](https://docs.kernel.org/admin-guide/README.html),
[APT simulation and package removal](https://manpages.debian.org/trixie/apt/apt-get.8.en.html).

Code license: GPL-3.0-or-later; see [LICENSE](LICENSE).
