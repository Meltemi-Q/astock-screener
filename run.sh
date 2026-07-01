#!/usr/bin/env bash
# 全球多市场五层选股 · 一键运行
# 用法：
#   ./run.sh                 普通运行 → 自动启动服务 + 打开浏览器
#   ./run.sh --fresh         强制刷新行情
#   ./run.sh --market cn|hk|us|all  选择市场（默认: cn，保持向后兼容）
#   ./run.sh --global        全市场筛选（--market all 的快捷方式）
#   ./run.sh --deep          选股后自动生成个股深度研报（含 AI 定性）
#   ./run.sh --deep --no-llm 深度研报但不调用 AI（仅量化数据）
#   ./run.sh --deep --ai-only 只对已有研报补 AI
#   ./run.sh --code 600519   单独生成某只股票的深度研报
#   ./run.sh --cbond         生成可转债双低策略筛选
#   ./run.sh --serve-only    仅启动服务（不跑选股）
#   ./run.sh --year 2024     使用 2024 年报
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
用法:
  ./run.sh                      普通运行 → 自动启动服务 + 打开浏览器
  ./run.sh --fresh              强制刷新行情
  ./run.sh --quotes-fresh       仅刷新行情缓存，财报复用
  ./run.sh --market cn|hk|us|all  选择市场（默认: cn，保持向后兼容）
  ./run.sh --global             全市场筛选（--market all 的快捷方式）
  ./run.sh --deep               选股后自动生成个股深度研报（含 AI 定性）
  ./run.sh --deep --no-llm      深度研报但不调用 AI（仅量化数据）
  ./run.sh --deep --ai-only     只对已有研报补 AI，不重抓财务/K线
  ./run.sh --deep --parallel=20 --ai-concurrency=20
  ./run.sh --code 600519        单独生成某只股票的深度研报
  ./run.sh --cbond              生成可转债双低策略筛选
  ./run.sh --serve-only         仅启动服务（不跑选股）
  ./run.sh --serve=8900         指定 HTTP 服务端口
  ./run.sh --no-serve           只跑选股，不启动服务
  ./run.sh --year 2024          使用 2024 年报
  ./run.sh --help               显示帮助
EOF
}

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then echo "❌ 未找到 python3"; exit 1; fi

# ── 解析参数 ──
SCREENER_ARGS=()
DEEP_ARGS=()
CBOND_ARGS=()
DO_SCREENER=true
DO_DEEP=false
DO_CBOND=false
DO_SERVE=true       # 默认启动服务（离线→在线）
SERVE_PORT=8899
REUSE_SERVER=false
EXPECT_CODE=""
EXPECT_YEAR=""
EXPECT_MARKET=""    # 期待 --market 后面的值

# 市场选择: 空 = 向后兼容 (使用 astock_screener.py)
MARKET=""
USE_GLOBAL=false    # 是否使用 global_screener.py

for arg in "$@"; do
  if [ -n "$EXPECT_CODE" ]; then
    DEEP_ARGS+=(--code "$arg"); DO_DEEP=true; DO_SCREENER=false; EXPECT_CODE=""; continue
  fi
  if [ -n "$EXPECT_YEAR" ]; then
    SCREENER_ARGS+=(--year "$arg"); EXPECT_YEAR=""; continue
  fi
  if [ -n "$EXPECT_MARKET" ]; then
    MARKET="$arg"; USE_GLOBAL=true; EXPECT_MARKET=""; continue
  fi

  case "$arg" in
    -h|--help)   usage; exit 0 ;;
    --fresh)     SCREENER_ARGS+=("$arg"); CBOND_ARGS+=("$arg") ;;
    --quotes-fresh) SCREENER_ARGS+=("$arg") ;;
    --deep)      DO_DEEP=true ;;
    --no-llm)    DEEP_ARGS+=("$arg") ;;
    --ai-only)   DEEP_ARGS+=("$arg"); DO_DEEP=true; DO_SCREENER=false ;;
    --no-kline)  DEEP_ARGS+=("$arg") ;;
    --parallel=*) DEEP_ARGS+=("--parallel" "${arg#*=}") ;;
    --ai-concurrency=*) DEEP_ARGS+=("--ai-concurrency" "${arg#*=}") ;;
    --prefetch-financials=*) DEEP_ARGS+=("--prefetch-financials" "${arg#*=}") ;;
    --code)      EXPECT_CODE="1" ;;
    --cbond)     DO_CBOND=true; DO_SCREENER=false ;;
    --year)      EXPECT_YEAR="1"; SCREENER_ARGS+=("$arg") ;;
    --market)    EXPECT_MARKET="1" ;;
    --global)    MARKET="all"; USE_GLOBAL=true ;;
    --serve-only) DO_SCREENER=false; DO_SERVE=true ;;
    --serve=*)   DO_SERVE=true; SERVE_PORT="${arg#*=}" ;;
    --no-serve)  DO_SERVE=false ;;
    *)           echo "❌ 未知参数: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -n "$EXPECT_CODE" ]; then echo "❌ --code 需要6位代码，例如: ./run.sh --code 600519" >&2; exit 1; fi
if [ -n "$EXPECT_YEAR" ]; then echo "❌ --year 需要年份，例如: ./run.sh --year 2024" >&2; exit 1; fi
if [ -n "$EXPECT_MARKET" ]; then echo "❌ --market 需要市场代码，例如: ./run.sh --market cn" >&2; exit 1; fi

# ── 验证 market 值 ──
if [ "$USE_GLOBAL" = true ]; then
  case "$MARKET" in
    cn|hk|us|all) ;;
    *) echo "❌ 无效市场: $MARKET (有效值: cn, hk, us, all)" >&2; exit 1 ;;
  esac
fi

# ── 中断时清理 server 进程 ──
cleanup() { [ -n "${SVR_PID:-}" ] && kill "$SVR_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

stop_existing_project_servers() {
  local ports=()
  local port pid cmd proc_cwd
  for port in $(seq "$SERVE_PORT" $((SERVE_PORT + 10))) 18899; do
    ports+=("$port")
  done

  for port in "${ports[@]}"; do
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
      case "$cmd" in
        *"server.py --port"*) ;;
        *) continue ;;
      esac

      proc_cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/{sub(/^n/,"");print;exit}')
      if [ "$proc_cwd" = "$PWD" ]; then
        echo "  ● 停止旧服务: PID $pid (port $port)"
        kill "$pid" 2>/dev/null || true
      fi
    done < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  done
}

# ── Step 0: 可转债双低策略 ──
if [ "$DO_CBOND" = true ]; then
  echo "========================================="
  echo "▶ 可转债双低策略筛选"
  echo "========================================="
  "$PY" cbond_double_low.py --jisilu-check ${CBOND_ARGS[@]+"${CBOND_ARGS[@]}"}
  TS=$(date +%Y%m%d)
  echo "─────────────────────────────────────────────"
  [ -f "results/cbond_double_low.html" ] && echo "🌐 固定入口: $(pwd)/results/cbond_double_low.html"
  [ -f "results/cbond_double_low_${TS}.html" ] && echo "🌐 总表: $(pwd)/results/cbond_double_low_${TS}.html"
  [ -f "results/cbond_double_low_${TS}.md" ] && echo "📄 结论: $(pwd)/results/cbond_double_low_${TS}.md"
  [ -f "results/cbond_double_low_${TS}.csv" ] && echo "📊 CSV:  $(pwd)/results/cbond_double_low_${TS}.csv"
fi

# ── Step 1: 五层选股 ──
if [ "$DO_SCREENER" = true ]; then
  echo "========================================="
  if [ "$USE_GLOBAL" = true ]; then
    echo "▶ Step 1/2: 全球多市场五层选股 ($MARKET)"
    echo "========================================="
    "$PY" global_screener.py --market "$MARKET" ${SCREENER_ARGS[@]+"${SCREENER_ARGS[@]}"}
  else
    echo "▶ Step 1/2: A股五层选股流水线"
    echo "========================================="
    "$PY" astock_screener.py ${SCREENER_ARGS[@]+"${SCREENER_ARGS[@]}"}
  fi

  TS=$(date +%Y%m%d)
  echo ""
  echo "─────────────────────────────────────────────"

  if [ "$USE_GLOBAL" = true ]; then
    case "$MARKET" in
      cn)
        [ -f "results/astock_screen.html" ] && echo "🌐 固定入口: $(pwd)/results/astock_screen.html"
        [ -f "results/astock_screen_${TS}.html" ] && echo "🌐 总表: $(pwd)/results/astock_screen_${TS}.html"
        [ -f "results/astock_shortlist_${TS}.md" ]  && echo "📄 榜单: $(pwd)/results/astock_shortlist_${TS}.md"
        [ -f "results/astock_screen_${TS}.csv" ]    && echo "📊 CSV:  $(pwd)/results/astock_screen_${TS}.csv"
        ;;
      hk)
        [ -f "results/hkstock_screen.html" ] && echo "🌐 固定入口: $(pwd)/results/hkstock_screen.html"
        [ -f "results/hkstock_screen_${TS}.html" ] && echo "🌐 总表: $(pwd)/results/hkstock_screen_${TS}.html"
        [ -f "results/hkstock_shortlist_${TS}.md" ]  && echo "📄 榜单: $(pwd)/results/hkstock_shortlist_${TS}.md"
        [ -f "results/hkstock_screen_${TS}.csv" ]    && echo "📊 CSV:  $(pwd)/results/hkstock_screen_${TS}.csv"
        ;;
      us)
        [ -f "results/usstock_screen.html" ] && echo "🌐 固定入口: $(pwd)/results/usstock_screen.html"
        [ -f "results/usstock_screen_${TS}.html" ] && echo "🌐 总表: $(pwd)/results/usstock_screen_${TS}.html"
        [ -f "results/usstock_shortlist_${TS}.md" ]  && echo "📄 榜单: $(pwd)/results/usstock_shortlist_${TS}.md"
        [ -f "results/usstock_screen_${TS}.csv" ]    && echo "📊 CSV:  $(pwd)/results/usstock_screen_${TS}.csv"
        ;;
      all)
        echo "🌐 统一入口: http://localhost:$SERVE_PORT/screen.html"
        [ -f "results/astock_screen.html" ] && echo "  🇨🇳 A股固定入口: $(pwd)/results/astock_screen.html"
        [ -f "results/hkstock_screen.html" ] && echo "  🇭🇰 港股固定入口: $(pwd)/results/hkstock_screen.html"
        [ -f "results/usstock_screen.html" ] && echo "  🇺🇸 美股固定入口: $(pwd)/results/usstock_screen.html"
        ;;
    esac
  else
    [ -f "results/astock_screen.html" ] && echo "🌐 固定入口: $(pwd)/results/astock_screen.html"
    [ -f "results/astock_screen_${TS}.html" ] && echo "🌐 总表: $(pwd)/results/astock_screen_${TS}.html"
    [ -f "results/astock_shortlist_${TS}.md" ]  && echo "📄 榜单: $(pwd)/results/astock_shortlist_${TS}.md"
    [ -f "results/astock_screen_${TS}.csv" ]    && echo "📊 CSV:  $(pwd)/results/astock_screen_${TS}.csv"
  fi
fi

# ── Step 2: 深度研报（可选）──
if [ "$DO_DEEP" = true ]; then
  if [ "$USE_GLOBAL" = true ] && [ "$MARKET" != "cn" ] && [ "$MARKET" != "all" ]; then
    echo ""
    echo "⚠️  港股/美股深度研报暂未实现，跳过深度研报步骤"
  elif [ "$USE_GLOBAL" = true ] && [ "$MARKET" = "all" ]; then
    echo ""
    echo "⚠️  --market all 模式下深度研报仅针对 A 股（港股/美股深度研报暂未实现）"
  fi

  # 只有 CN 市场或向后兼容模式才运行深度研报
  if [ "$USE_GLOBAL" = false ] || [ "$MARKET" = "cn" ] || [ "$MARKET" = "all" ]; then
    echo ""
    echo "========================================="
    echo "▶ Step 2/2: 个股深度研报"
    echo "========================================="
    "$PY" stock_deep_dive.py ${DEEP_ARGS[@]+"${DEEP_ARGS[@]}"}
    if [ "$DO_SCREENER" = true ]; then
      echo ""; echo "▶ 刷新总表链接..."
      if [ "$USE_GLOBAL" = true ]; then
        "$PY" global_screener.py --market cn
      else
        "$PY" astock_screener.py
      fi
    fi
    echo "─────────────────────────────────────────────"
    [ -f "results/deep_dives/index.html" ] && echo "🔬 研报索引: $(pwd)/results/deep_dives/index.html"
  fi
fi

# ── 启动本地 HTTP 服务 + 打开浏览器 ──
if [ "$DO_SERVE" = true ]; then
  stop_existing_project_servers
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

  # 选择打开的页面
  OPEN_PAGE="astock_screen.html"
  if [ "$USE_GLOBAL" = true ]; then
    OPEN_PAGE="screen.html"
  elif [ "$DO_CBOND" = true ]; then
    OPEN_PAGE="cbond_double_low.html"
  fi

  echo "  http://localhost:$SERVE_PORT/$OPEN_PAGE"

  # 用浏览器打开（通过 http:// 而非 file://，使所有按钮可用）
  if [ "$(uname)" = "Darwin" ]; then
    if [ "$USE_GLOBAL" = true ]; then
      open "http://localhost:$SERVE_PORT/$OPEN_PAGE" 2>/dev/null || true
    elif [ "$DO_CBOND" = true ]; then
      open "http://localhost:$SERVE_PORT/$OPEN_PAGE" 2>/dev/null || true
    else
      if [ -f "results/astock_screen.html" ] || [ -n "$(find results -maxdepth 1 -name 'astock_screen_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].html' -print -quit 2>/dev/null)" ]; then
        open "http://localhost:$SERVE_PORT/astock_screen.html" 2>/dev/null || true
      else
        open "http://localhost:$SERVE_PORT" 2>/dev/null || true
      fi
    fi
  fi

  if [ "$REUSE_SERVER" = false ]; then
    echo "  Ctrl+C 停止"
    wait $SVR_PID 2>/dev/null || true
  fi
fi

echo ""
echo "✅ 全部完成"
