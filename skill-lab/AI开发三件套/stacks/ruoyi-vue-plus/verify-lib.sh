#!/usr/bin/env bash
# RuoYi-Vue-Plus 栈的 verify/accept 函数库（源自 一个实战项目scripts/verify-env.sh.example 泛化）
# 用法：项目的 scripts/verify-env.sh 先 export 环境变量，再 source 本文件：
#   export DB_NAME=ry_xxx DB_CONTAINER=dev-mysql DB_PASS=...
#   export API=http://localhost:8088 CLIENT_ID=... ADMIN_USER=admin ADMIN_PASS=...
#   source ~/.claude/stacks/ruoyi-vue-plus/verify-lib.sh
# 口令只出现在项目的 verify-env.sh（不进 git），本文件零凭证、可全局共享。
#
# ⚠️ 为什么全是函数不是变量：zsh 默认不做 word splitting，`$MYSQL -Nse ...` 会被当成
#    一整个命令名报 command not found（bash 下正常，最难查的环境差异）。gotchas §14.1。

db() {  # db "SELECT ..." → 无表头结果行
  docker exec "${DB_CONTAINER:?verify-env 未 export DB_CONTAINER}" \
    mysql -uroot -p"${DB_PASS:?}" "${DB_NAME:?}" \
    --default-character-set=utf8mb4 -Nse "$@" 2>/dev/null
}

dbdump() {
  docker exec "${DB_CONTAINER:?}" mysqldump -uroot -p"${DB_PASS:?}" \
    --default-character-set=utf8mb4 "$@" 2>/dev/null
}

# 登录拿 token。RuoYi 必须 clientId + grantType 双全：缺 clientId → 返回体没有
# access_token → 后续全部 401，而断言只看见"红"看不出根因（gotchas §14.2）。
login() {
  ADMIN_TOKEN=$(curl -sf --noproxy '*' -X POST "${API:?}${LOGIN_PATH:-/auth/login}" \
    -H 'Content-Type: application/json' -H "clientid: ${CLIENT_ID:?}" \
    -d "{\"username\":\"${ADMIN_USER:?}\",\"password\":\"${ADMIN_PASS:?}\",\"clientId\":\"${CLIENT_ID}\",\"grantType\":\"password\"}" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["access_token"])') \
    && export ADMIN_TOKEN
}

api() {  # api /path [curl 额外参数...] → 响应体（未 login 会因 :? 立即报错而不是静默 401）
  curl -sf --noproxy '*' -H "Authorization: Bearer ${ADMIN_TOKEN:?先跑 login}" \
    -H "clientid: ${CLIENT_ID:?}" "${API:?}$1" "${@:2}"
}

json() {  # ... | json 'd["rows"][0]["slug"]' → 从 stdin JSON 取值
  python3 -c 'import json,sys;print(eval(sys.argv[1],{"d":json.load(sys.stdin)}))' "$1"
}
