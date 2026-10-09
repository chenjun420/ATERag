"""板卡 SSH 命令执行器 — 部署/排障通用工具.

用法 (单次):
    .venv\\Scripts\\python.exe scripts\\board_ssh.py "uname -a"
    .venv\\Scripts\\python.exe scripts\\board_ssh.py --sudo "systemctl status postgresql"
    .venv\\Scripts\\python.exe scripts\\board_ssh.py --put local.txt /remote/path
    .venv\\Scripts\\python.exe scripts\\board_ssh.py --get /remote/path local.txt

凭据: BOARD_SSH_HOST / BOARD_SSH_USER / BOARD_SSH_KEY / BOARD_SSH_PASSWORD
**key 优先**: 板卡配了公钥认证, 默认走 ``~/.ssh/id_rsa_aterag``; 只有在没有 key
时才回落到密码。密码只从环境变量读, 绝不写入仓库。

为什么默认用户名是 ``rpdzkj`` 而不是 ``aterag``
----------------------------------------------
``.env.example`` 曾写 ``BOARD_SSH_USER=aterag``, 但板卡上的实际账号是
``rpdzkj`` —— 该用户没有 ``aterag`` 这个系统账号, 于是照 example 配的每一台
机器都会连不上, 而报错是 ``Permission denied (publickey,password)``: 看不出
「用户名错了」还是「key 不对」。这里的默认值取实测可连通的账号。

``BOARD_SSH_KEY`` 默认走 ``~/.ssh/config`` 里已配好的 ``aterag-board`` 别名 ——
那份 config 里已经绑定了 ``HostName``/``User``/``IdentityFile``, 复用它比在
本脚本里重述一遍 host/user/key 三元组更可靠(那三元组本来就该只有一处)。
"""

from __future__ import annotations

import os
import posixpath
import shlex
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.25")
USER = os.getenv("BOARD_SSH_USER", "rpdzkj")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")
#: 私钥路径。**默认显式指向 ``id_rsa_aterag``, 不能靠 paramiko 的默认搜索**:
#: paramiko 不读 ``~/.ssh/config``, 所以 ssh 命令能用的 ``aterag-board`` 别名在
#: 这里无效; 而它的默认搜索只找 ``id_rsa``/``id_dsa``/``id_ecdsa`` 这类默认名,
#: 叫 ``id_rsa_aterag`` 的专用 key 不会被自动发现 —— 表现为「明明装了 key 却说
#: 认证失败」。
DEFAULT_KEY = "~/.ssh/id_rsa_aterag"
KEY = os.getenv("BOARD_SSH_KEY", "") or (
    DEFAULT_KEY if os.path.isfile(os.path.expanduser(DEFAULT_KEY)) else ""
)


def connect() -> paramiko.SSHClient:
    """建连。key 可用就走 key, 否则回落密码。

    **key 失败时要说清是哪一种失败** —— 笼统的 ``Permission denied`` 会让人
    以为是密码错, 于是去改密码, 而实际是公钥没装到板卡上。
    """
    have_key = bool(KEY) or os.path.isfile(os.path.expanduser(DEFAULT_KEY))
    if not PWD and not have_key:
        sys.exit(
            "没有可用的 SSH 凭据: 设 BOARD_SSH_KEY 指向私钥, 或设 BOARD_SSH_PASSWORD。"
            "(板卡已配公钥认证, 优先用 key)"
        )
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {
        "hostname": HOST,
        "username": USER,
        "timeout": 20,
        "look_for_keys": bool(KEY),
        "allow_agent": bool(KEY),
    }
    if KEY:
        kwargs["key_filename"] = os.path.expanduser(KEY)
    elif PWD:
        kwargs["password"] = PWD
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    try:
        cli.connect(**kwargs)
    except paramiko.AuthenticationException as e:
        sys.exit(
            f"SSH 认证失败 ({HOST} user={USER}): {e}\n"
            f"  用的是 {'key ' + KEY if KEY else ('默认 key 搜索' if not PWD else '密码')}。"
            f"\n  key 认证失败通常意味着板卡上没有该公钥 —— 用密码登一次后执行:\n"
            f"    mkdir -p ~/.ssh && cat id_rsa_aterag.pub >> ~/.ssh/authorized_keys"
        )
    return cli


def run_cmd(cmd: str, use_sudo: bool = False) -> int:
    cli = connect()
    try:
        # 关键: sudo 必须包裹整条命令链。`sudo -n a && b` 只给 a 提权, b 仍以 SSH
        # 用户身份运行 (chown 就会报 "Operation not permitted")。
        full = f"sudo -n sh -c {shlex.quote(cmd)}" if use_sudo else cmd
        _, out, err = cli.exec_command(full, timeout=1800)
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if so:
            sys.stdout.write(so)
        if se:
            sys.stderr.write(se)
        return rc
    finally:
        cli.close()


def put(local: str, remote: str) -> int:
    """上传一个文件(必要时先建父目录)。

    **``mkdir -p`` 的退出码必须检查。** 原来它被丢弃, 于是「建目录失败」与
    「目录已存在」走到同一条路径, 紧接着 ``sftp.put`` 报
    ``FileNotFoundError: No such file`` —— 看着像**本地文件或远端路径不存在**,
    真实原因往往是没权限(``/opt/aterag`` 属主是服务账号 ``aterag``, 而部署
    是用 SSH 账号 ``rpdzkj`` 跑的)。这个报错会把人引到完全错误的方向。
    """
    cli = connect()
    try:
        sftp = cli.open_sftp()
        try:
            parent = posixpath.dirname(remote)
            if parent:
                _, pout, perr = cli.exec_command(f"mkdir -p {parent}")
                rc = pout.channel.recv_exit_status()
                if rc != 0:
                    msg = (perr.read().decode("utf-8", "replace") or "").strip()
                    sys.exit(
                        f"无法创建远端目录 {parent} (rc={rc})。\n"
                        f"  {msg}\n"
                        f"  当前 SSH 用户: {USER}@{HOST}\n"
                        f"  —— 多半是权限: /opt/aterag 属服务账号 {HOST} 上的 "
                        f"aterag, 而部署用 SSH 账号跑。需要 sudo 建目录后再传。"
                    )
            sftp.put(local, remote)
        finally:
            sftp.close()
        print(f"PUT {local} -> {remote}")
        return 0
    finally:
        cli.close()


def get(remote: str, local: str) -> int:
    cli = connect()
    try:
        sftp = cli.open_sftp()
        try:
            sftp.get(remote, local)
        finally:
            sftp.close()
        print(f"GET {remote} -> {local}")
        return 0
    finally:
        cli.close()


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--put" and len(args) == 3:
        return put(args[1], args[2])
    if args[0] == "--get" and len(args) == 3:
        return get(args[1], args[2])
    use_sudo = False
    if args[0] == "--sudo":
        use_sudo, args = True, args[1:]
    return run_cmd(" ".join(args), use_sudo)


if __name__ == "__main__":
    raise SystemExit(main())
