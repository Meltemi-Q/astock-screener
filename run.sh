#!/usr/bin/env bash
# A股五层选股 · 一键运行
# 用法：
#   ./run.sh                 普通运行 → 自动启动服务 + 打开浏览器
#   ./run.sh --fresh         强制刷新行情
#   ./run.sh --deep          选股后自动生成个股深度研报（含 AI 定性）
#   ./run.sh --deep --no-llm 深度研报但不调用 AI（仅量化数据）
#   ./run.sh --code 600519   单独生成某只股票的深度研报
#   ./run.sh --serve-only    仅启动服务（不跑选股）
#   ./run.sh --year 2024     使用 2024 年报
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
用法:
  ./run.sh                 普通运行 → 自动启动服务 + 打开浏览器
  ./run.sh --fresh         强制刷新行情
  ./run.sh --deep          选股后自动生成个股深度研报（含 AI 定性）
  ./run.sh --deep --no-llm 深度研报但不调用 AI（仅量化数据）
  ./run.sh --code 600519   单独生成某只股票的深度研报
  ./run.sh --serve-only    仅启动服务（不跑选股）
  ./run.sh --serve=8900    指定 HTTP 服务端口
  ./run.sh --no-serve      只跑选股，不启动服务
  ./run.sh --year 2024     使用 2024 年报
  ./run.sh --help          显示帮助
EOF
}

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then echo "❌ 未找到 python3"; exit 1; fi

# ── 解析参数 ──
SCREENER_ARGS=()
DEEP_ARGS=()
DO_SCREENER=true
DO_DEEP=false
DO_SERVE=true       # 默认启动服务（离线→在线）
SERVE_PORT=8899
REUSE_SERVER=false
EXPECT_CODE=""
EXPECT_YEAR=""

for arg in "$@"; do
  if [ -n "$EXPECT_CODE" ]; then
    DEEP_ARGS+=(--code "$arg"); DO_DEEP=true; DO_SCREENER=false; EXPECT_CODE=""; continue
  fi
  if [ -n "$EXPECT_YEAR" ]; then
    SCREENER_ARGS+=(--year "$arg"); EXPECT_YEAR=""; continue
  fi

  case "$arg" in
    -h|--help)   usage; exit 0 ;;
    --fresh)     SCREENER_ARGS+=("$arg") ;;
    --deep)      DO_DEEP=true ;;
    --no-llm)    DEEP_ARGS+=("$arg") ;;
    --code)      EXPECT_CODE="1" ;;
    --year)      EXPECT_YEAR="1"; SCREENER_ARGS+=("$arg") ;;
    --serve-only) DO_SCREENER=false; DO_SERVE=true ;;
    --serve=*)   DO_SERVE=true; SERVE_PORT="${arg#*=}" ;;
    --no-serve)  DO_SERVE=false ;;
    *)           echo "❌ 未知参数: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -n "$EXPECT_CODE" ]; then echo "❌ --code 需要6位代码，例如: ./run.sh --code 600519" >&2; exit 1; fi
if [ -n "$EXPECT_YEAR" ]; then echo "❌ --year 需要年份，例如: ./run.sh --year 2024" >&2; exit 1; fi

# ── 中断时清理 server 进程 ──
cleanup() { [ -n "${SVR_PID:-}" ] && kill "$SVR_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# ── Step 1: 五层选股 ──
if [ "$DO_SCREENER" = true ]; then
  echo "========================================="
  echo "▶ Step 1/2: A股五层选股流水线"
  echo "========================================="
  "$PY" astock_screener.py ${SCREENER_ARGS[@]+"${SCREENER_ARGS[@]}"}

  TS=$(date +%Y%m%d)
  echo ""
  echo "─────────────────────────────────────────────"
  [ -f "results/astock_screen_${TS}.html" ] && echo "🌐 总表: $(pwd)/results/astock_screen_${TS}.html"
  [ -f "results/astock_shortlist_${TS}.md" ]  && echo "📄 榜单: $(pwd)/results/astock_shortlist_${TS}.md"
  [ -f "results/astock_screen_${TS}.csv" ]    && echo "📊 CSV:  $(pwd)/results/astock_screen_${TS}.csv"
fi

# ── Step 2: 深度研报（可选）──
if [ "$DO_DEEP" = true ]; then
  echo ""
  echo "========================================="
  echo "▶ Step 2/2: 个股深度研报"
  echo "========================================="
  "$PY" stock_deep_dive.py ${DEEP_ARGS[@]+"${DEEP_ARGS[@]}"}
  if [ "$DO_SCREENER" = true ]; then
    echo ""; echo "▶ 刷新总表链接..."; "$PY" astock_screener.py
  fi
  echo "─────────────────────────────────────────────"
  [ -f "results/deep_dives/index.html" ] && echo "🔬 研报索引: $(pwd)/results/deep_dives/index.html"
fi

# ── 启动本地 HTTP 服务 + 打开浏览器 ──
if [ "$DO_SERVE" = true ]; then
  START_PORT=$SERVE_PORT
  for try_port in $(seq "$SERVE_PORT" $((SERVE_PORT + 10))); do
    if lsof -nP -iTCP:"$try_port" -sTCP:LISTEN >/dev/null 2>&1; then
      if curl -fsS "http://localhost:$try_port/api/status" >/dev/null 2>&1; then
        SERVE_PORT=$try_port
        REUSE_SERVER=true
        break
      fi
      continue
    fi
    SERVE_PORT=$try_port
    break
  done

  if [ "$START_PORT" != "$SERVE_PORT" ]; then
    echo "⚠️  端口 $START_PORT 被占用，改用 $SERVE_PORT"
  fi

  echo ""
  echo "========================================="
  echo "▶ 启动本地服务 (端口 $SERVE_PORT)"
  echo "========================================="

  if [ "$REUSE_SERVER" = true ]; then
    echo "  ● 复用现有服务 — 按钮可用：🔄刷新 · ⚡一键研报 · 🧠定性分析"
  else
    "$PY" server.py --port "$SERVE_PORT" &
    SVR_PID=$!
    sleep 1.5

    if ! kill -0 $SVR_PID 2>/dev/null; then
      echo "❌ 服务启动失败"; exit 1
    fi
    echo "  ● 已连接 — 按钮可用：🔄刷新 · ⚡一键研报 · 🧠定性分析"
  fi

  echo "  http://localhost:$SERVE_PORT"

  # 用浏览器打开（通过 http:// 而非 file://，使所有按钮可用）
  TS=$(date +%Y%m%d)
  if [ "$(uname)" = "Darwin" ]; then
    if [ -f "results/astock_screen_${TS}.html" ]; then
      open "http://localhost:$SERVE_PORT/astock_screen_${TS}.html" 2>/dev/null || true
    else
      open "http://localhost:$SERVE_PORT" 2>/dev/null || true
    fi
  fi

  if [ "$REUSE_SERVER" = false ]; then
    echo "  Ctrl+C 停止"
    wait $SVR_PID 2>/dev/null || true
  fi
fi

echo ""
echo "✅ 全部完成"
