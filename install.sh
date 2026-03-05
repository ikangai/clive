#!/usr/bin/env bash
set -euo pipefail

# ── clive installer ──────────────────────────────────────────────────────────
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/ikangai/clive/main/install.sh | bash
#   # or after cloning:
#   bash install.sh
#
# What it does:
#   1. Checks prerequisites (Python 3.10+, tmux)
#   2. Clones repo (if not already in it)
#   3. Creates venv and installs Python deps
#   4. Offers to install CLI tools for selected profile
#   5. Sets up .env for LLM provider
#   6. Creates 'clive' launcher script
# ─────────────────────────────────────────────────────────────────────────────

REPO_URL="https://github.com/ikangai/clive.git"
INSTALL_DIR="${CLIVE_HOME:-$HOME/.clive}"
BIN_DIR="${HOME}/.local/bin"
MIN_PYTHON="3.10"

# ── Colors ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

logo() {
    echo -e "${CYAN}"
    cat <<'ART'
 ██████╗██╗     ██╗██╗   ██╗███████╗
██╔════╝██║     ██║██║   ██║██╔════╝
██║     ██║     ██║██║   ██║█████╗
██║     ██║     ██║╚██╗ ██╔╝██╔══╝
╚██████╗███████╗██║ ╚████╔╝ ███████╗
 ╚═════╝╚══════╝╚═╝  ╚═══╝  ╚══════╝
ART
    echo -e "${RESET}"
    echo -e "${DIM}CLI Live Environment — installer${RESET}"
    echo
}

info()  { echo -e "${GREEN}[+]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
err()   { echo -e "${RED}[x]${RESET} $*"; }
ask()   { echo -en "${CYAN}[?]${RESET} $* "; }

# ── Platform detection ───────────────────────────────────────────────────────

detect_platform() {
    OS="$(uname -s)"
    case "${OS}" in
        Darwin) PLATFORM="macos" ;;
        Linux)  PLATFORM="linux" ;;
        MINGW*|MSYS*|CYGWIN*)
            echo
            err "Native Windows is not supported."
            echo
            echo "  clive requires tmux, which is not available on Windows."
            echo "  Install WSL (Windows Subsystem for Linux) and run this"
            echo "  installer inside your WSL terminal:"
            echo
            echo "    1. Open PowerShell as admin and run:"
            echo "       wsl --install"
            echo "    2. Restart, open Ubuntu from the Start menu"
            echo "    3. Run this installer inside WSL:"
            echo "       curl -sSL https://raw.githubusercontent.com/ikangai/clive/main/install.sh | bash"
            echo
            exit 1
            ;;
        *)
            warn "Unknown OS: ${OS} — proceeding anyway"
            PLATFORM="linux"
            ;;
    esac

    # Detect package manager
    PKG_MGR=""
    if command -v brew &>/dev/null; then
        PKG_MGR="brew"
    elif command -v apt-get &>/dev/null; then
        PKG_MGR="apt"
    elif command -v dnf &>/dev/null; then
        PKG_MGR="dnf"
    elif command -v pacman &>/dev/null; then
        PKG_MGR="pacman"
    fi

    info "Platform: ${PLATFORM} (package manager: ${PKG_MGR:-none})"
}

# ── Prerequisites ────────────────────────────────────────────────────────────

check_prerequisites() {
    local missing=()

    # Python
    if command -v python3 &>/dev/null; then
        local pyver
        pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        if python3 -c "
import sys
min_parts = [int(x) for x in '$MIN_PYTHON'.split('.')]
if sys.version_info[:2] >= tuple(min_parts):
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
            info "Python ${pyver} found"
        else
            err "Python ${pyver} found but ${MIN_PYTHON}+ required"
            missing+=(python3)
        fi
    else
        err "Python 3 not found"
        missing+=(python3)
    fi

    # tmux
    if command -v tmux &>/dev/null; then
        info "tmux $(tmux -V | cut -d' ' -f2) found"
    else
        err "tmux not found"
        missing+=(tmux)
    fi

    # git
    if command -v git &>/dev/null; then
        info "git found"
    else
        err "git not found"
        missing+=(git)
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo
        err "Missing prerequisites: ${missing[*]}"
        echo
        case "${PKG_MGR}" in
            brew)   echo "  brew install ${missing[*]}" ;;
            apt)    echo "  sudo apt install ${missing[*]}" ;;
            dnf)    echo "  sudo dnf install ${missing[*]}" ;;
            pacman) echo "  sudo pacman -S ${missing[*]}" ;;
        esac
        echo
        exit 1
    fi
    echo
}

# ── Clone or detect repo ────────────────────────────────────────────────────

setup_repo() {
    # Are we already inside the clive repo?
    if [[ -f "clive.py" && -f "toolsets.py" ]]; then
        INSTALL_DIR="$(pwd)"
        info "Already in clive repo: ${INSTALL_DIR}"
        return
    fi

    if [[ -d "${INSTALL_DIR}" && -f "${INSTALL_DIR}/clive.py" ]]; then
        info "clive already installed at ${INSTALL_DIR}"
        ask "Update? [Y/n]"
        read -r answer
        if [[ "${answer,,}" != "n" ]]; then
            cd "${INSTALL_DIR}"
            git pull --ff-only || warn "Could not update — continuing with existing version"
        fi
        return
    fi

    info "Cloning clive to ${INSTALL_DIR}..."
    git clone "${REPO_URL}" "${INSTALL_DIR}"
    cd "${INSTALL_DIR}"
    info "Cloned to ${INSTALL_DIR}"
}

# ── Python venv ──────────────────────────────────────────────────────────────

setup_venv() {
    cd "${INSTALL_DIR}"

    if [[ -d ".venv" ]]; then
        info "Virtual environment already exists"
    else
        info "Creating virtual environment..."
        python3 -m venv .venv
    fi

    info "Installing Python dependencies..."
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
    info "Python dependencies installed"
    echo
}

# ── Profile selection & CLI tool install ─────────────────────────────────────

select_profile() {
    echo -e "${BOLD}Which toolset profile do you want to set up?${RESET}"
    echo
    echo "  1) minimal     — shell only (zero extra installs)"
    echo "  2) standard    — shell + browser + data + docs"
    echo "  3) full        — everything: email, calendar, tasks, media, search"
    echo "  4) research    — web, data, docs, media, search, AI"
    echo "  5) business    — web, data, docs, email, tasks, finance"
    echo "  6) creative    — web, media, images, AI, translation"
    echo "  7) skip        — don't install CLI tools now"
    echo
    ask "Choose [1-7, default: 2]:"
    read -r choice

    case "${choice}" in
        1) PROFILE="minimal" ;;
        3) PROFILE="full" ;;
        4) PROFILE="research" ;;
        5) PROFILE="business" ;;
        6) PROFILE="creative" ;;
        7) PROFILE="skip" ;;
        *) PROFILE="standard" ;;
    esac

    if [[ "${PROFILE}" == "skip" ]]; then
        info "Skipping CLI tool installation"
        echo
        return
    fi

    info "Setting up profile: ${PROFILE}"
    echo

    install_tools_for_profile
}

install_tools_for_profile() {
    if [[ -z "${PKG_MGR}" ]]; then
        warn "No supported package manager found (brew, apt, dnf, or pacman)"
        warn "Install CLI tools manually — see TOOLS.md"
        echo
        return
    fi

    # Determine what to install based on profile.
    # We use generic tool names and map them per package manager below.
    local tools=()
    local pip_pkgs=()

    case "${PROFILE}" in
        standard)
            tools=(lynx ripgrep jq pandoc miller poppler)
            ;;
        full)
            tools=(lynx ripgrep jq pandoc miller poppler neomutt ical-buddy task ddgr)
            pip_pkgs=(yt-dlp)
            ;;
        research)
            tools=(lynx ripgrep jq pandoc miller poppler ddgr)
            pip_pkgs=(yt-dlp)
            ;;
        business)
            tools=(lynx ripgrep jq pandoc miller poppler neomutt ical-buddy task hledger)
            ;;
        creative)
            tools=(lynx ripgrep jq imagemagick exiftool translate-shell)
            pip_pkgs=(yt-dlp)
            ;;
        minimal)
            info "Minimal profile — no extra tools needed"
            echo
            return
            ;;
    esac

    # Map generic tool names to actual package names per package manager.
    # Also map to binary names for installed-check.
    pkg_name_for() {
        local tool="$1"
        case "${PKG_MGR}" in
            brew)
                echo "${tool}"  # brew names match our generic names
                ;;
            apt)
                case "${tool}" in
                    ripgrep)         echo "ripgrep" ;;
                    miller)          echo "miller" ;;
                    poppler)         echo "poppler-utils" ;;
                    ical-buddy)      echo "" ;;  # macOS only
                    task)            echo "taskwarrior" ;;
                    imagemagick)     echo "imagemagick" ;;
                    translate-shell) echo "translate-shell" ;;
                    exiftool)        echo "libimage-exiftool-perl" ;;
                    hledger)         echo "hledger" ;;
                    *)               echo "${tool}" ;;
                esac
                ;;
            dnf)
                case "${tool}" in
                    ripgrep)         echo "ripgrep" ;;
                    miller)          echo "miller" ;;
                    poppler)         echo "poppler-utils" ;;
                    ical-buddy)      echo "" ;;
                    task)            echo "task" ;;
                    imagemagick)     echo "ImageMagick" ;;
                    translate-shell) echo "" ;;  # not in dnf, use pip
                    exiftool)        echo "perl-Image-ExifTool" ;;
                    hledger)         echo "" ;;  # not in dnf, use pip
                    ddgr)            echo "" ;;  # not in dnf, use pip
                    *)               echo "${tool}" ;;
                esac
                ;;
            pacman)
                case "${tool}" in
                    ripgrep)         echo "ripgrep" ;;
                    miller)          echo "miller" ;;
                    poppler)         echo "poppler" ;;
                    ical-buddy)      echo "" ;;
                    task)            echo "task" ;;
                    imagemagick)     echo "imagemagick" ;;
                    translate-shell) echo "translate-shell" ;;
                    exiftool)        echo "perl-image-exiftool" ;;
                    hledger)         echo "hledger" ;;
                    ddgr)            echo "ddgr" ;;
                    *)               echo "${tool}" ;;
                esac
                ;;
        esac
    }

    bin_name_for() {
        local tool="$1"
        case "${tool}" in
            ripgrep)         echo "rg" ;;
            miller)          echo "mlr" ;;
            poppler)         echo "pdftotext" ;;
            ical-buddy)      echo "icalBuddy" ;;
            translate-shell) echo "trans" ;;
            imagemagick)     echo "convert" ;;
            *)               echo "${tool}" ;;
        esac
    }

    # Filter out already-installed tools and resolve package names
    local to_install=()
    local pip_fallback=()
    for tool in "${tools[@]}"; do
        local bin
        bin=$(bin_name_for "${tool}")
        if command -v "${bin}" &>/dev/null; then
            echo -e "  ${GREEN}+${RESET} ${tool} already installed"
            continue
        fi
        local pkg
        pkg=$(pkg_name_for "${tool}")
        if [[ -n "${pkg}" ]]; then
            to_install+=("${pkg}")
        else
            # Tool not available in this package manager
            echo -e "  ${DIM}-${RESET} ${tool} ${DIM}(not available via ${PKG_MGR}, skipping)${RESET}"
        fi
    done

    local to_install_pip=()
    for pkg in "${pip_pkgs[@]}"; do
        if ! command -v "${pkg}" &>/dev/null; then
            to_install_pip+=("${pkg}")
        else
            echo -e "  ${GREEN}+${RESET} ${pkg} already installed"
        fi
    done

    echo

    # Install system packages
    if [[ ${#to_install[@]} -gt 0 ]]; then
        case "${PKG_MGR}" in
            brew)
                info "Installing via brew: ${to_install[*]}"
                brew install "${to_install[@]}" || warn "Some packages failed"
                ;;
            apt)
                info "Installing via apt: ${to_install[*]}"
                sudo apt-get update -qq
                sudo apt-get install -y "${to_install[@]}" || warn "Some packages failed"
                ;;
            dnf)
                info "Installing via dnf: ${to_install[*]}"
                sudo dnf install -y "${to_install[@]}" || warn "Some packages failed"
                ;;
            pacman)
                info "Installing via pacman: ${to_install[*]}"
                sudo pacman -S --noconfirm "${to_install[@]}" || warn "Some packages failed"
                ;;
        esac
    fi

    # Install pip packages into the venv
    if [[ ${#to_install_pip[@]} -gt 0 ]]; then
        info "Installing via pip: ${to_install_pip[*]}"
        "${INSTALL_DIR}/.venv/bin/pip" install --quiet "${to_install_pip[@]}" || warn "Some pip packages failed"
    fi

    # Special: offer whisper for full/research/creative profiles
    if [[ "${PROFILE}" =~ ^(full|research|creative)$ ]]; then
        if ! command -v whisper &>/dev/null; then
            echo
            ask "Install OpenAI Whisper for audio transcription? (large download) [y/N]:"
            read -r answer
            if [[ "${answer,,}" == "y" ]]; then
                # ffmpeg is needed by whisper
                case "${PKG_MGR}" in
                    brew)   brew install ffmpeg 2>/dev/null || true ;;
                    apt)    sudo apt-get install -y ffmpeg 2>/dev/null || true ;;
                    dnf)    sudo dnf install -y ffmpeg 2>/dev/null || true ;;
                    pacman) sudo pacman -S --noconfirm ffmpeg 2>/dev/null || true ;;
                esac
                info "Installing whisper..."
                "${INSTALL_DIR}/.venv/bin/pip" install --quiet openai-whisper || warn "Whisper install failed"
            fi
        fi
    fi

    echo
    info "CLI tools installed for ${PROFILE} profile"
    echo
}

# ── LLM provider setup ──────────────────────────────────────────────────────

setup_env() {
    cd "${INSTALL_DIR}"

    if [[ -f ".env" ]]; then
        info ".env already exists"
        ask "Reconfigure LLM provider? [y/N]:"
        read -r answer
        if [[ "${answer,,}" != "y" ]]; then
            echo
            return
        fi
    fi

    echo -e "${BOLD}Choose your LLM provider:${RESET}"
    echo
    echo "  1) OpenRouter    — multi-model gateway (recommended)"
    echo "  2) Anthropic     — Claude models directly"
    echo "  3) OpenAI        — GPT models"
    echo "  4) Google Gemini — Gemini models"
    echo "  5) LM Studio     — local models (no API key)"
    echo "  6) Ollama        — local models (no API key)"
    echo "  7) skip          — configure later"
    echo
    ask "Choose [1-7, default: 1]:"
    read -r choice

    local provider=""
    local key_var=""
    local key_prompt=""

    case "${choice}" in
        2) provider="anthropic";  key_var="ANTHROPIC_API_KEY";   key_prompt="Anthropic API key" ;;
        3) provider="openai";     key_var="OPENAI_API_KEY";      key_prompt="OpenAI API key" ;;
        4) provider="gemini";     key_var="GOOGLE_API_KEY";      key_prompt="Google API key" ;;
        5) provider="lmstudio" ;;
        6) provider="ollama" ;;
        7)
            if [[ ! -f ".env" ]]; then
                cp .env.example .env 2>/dev/null || true
            fi
            info "Skipped — edit .env when ready"
            echo
            return
            ;;
        *) provider="openrouter"; key_var="OPENROUTER_API_KEY";  key_prompt="OpenRouter API key" ;;
    esac

    local env_content="LLM_PROVIDER=${provider}"

    if [[ -n "${key_var:-}" ]]; then
        ask "${key_prompt}:"
        read -r api_key
        if [[ -n "${api_key}" ]]; then
            env_content="${env_content}\n${key_var}=${api_key}"
        else
            env_content="${env_content}\n${key_var}=sk-your-key-here"
            warn "No key entered — edit .env later"
        fi
    fi

    echo -e "${env_content}" > .env
    info "Saved .env (provider: ${provider})"
    echo
}

# ── Launcher script ──────────────────────────────────────────────────────────

create_launcher() {
    mkdir -p "${BIN_DIR}"

    # Main clive launcher
    cat > "${BIN_DIR}/clive" <<LAUNCHER
#!/usr/bin/env bash
exec "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/clive.py" "\$@"
LAUNCHER
    chmod +x "${BIN_DIR}/clive"

    # TUI launcher
    cat > "${BIN_DIR}/clive-tui" <<LAUNCHER
#!/usr/bin/env bash
exec "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/tui.py" "\$@"
LAUNCHER
    chmod +x "${BIN_DIR}/clive-tui"

    info "Created launchers:"
    echo "  ${BIN_DIR}/clive        — CLI mode"
    echo "  ${BIN_DIR}/clive-tui    — TUI mode"

    # Check if BIN_DIR is in PATH
    if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
        echo
        warn "${BIN_DIR} is not in your PATH"
        echo
        echo "  Add to your shell profile:"
        echo
        if [[ "${SHELL}" == */zsh ]]; then
            echo "    echo 'export PATH=\"${BIN_DIR}:\$PATH\"' >> ~/.zshrc"
            echo "    source ~/.zshrc"
        else
            echo "    echo 'export PATH=\"${BIN_DIR}:\$PATH\"' >> ~/.bashrc"
            echo "    source ~/.bashrc"
        fi
    fi
    echo
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    logo
    detect_platform
    check_prerequisites
    setup_repo
    setup_venv
    select_profile
    setup_env
    create_launcher

    echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
    echo
    echo "  Quick start:"
    echo "    clive \"list files in /tmp and summarize\"          # CLI mode"
    echo "    clive -t standard \"browse example.com\"            # with tools"
    echo "    clive-tui                                          # TUI mode"
    echo "    clive --list-tools                                 # see what's available"
    echo
    echo "  Watch the agent work:"
    echo "    tmux attach -t clive"
    echo
}

main "$@"
