"""Redis 缓存层：封装连接与字符串读写（SET/GET + TTL + 限流计数）。

职责：
- 从 config 读取 Redis 连接参数（不在这里硬编码）；
- 提供 get_client（连接）、cache_set/cache_get（带 TTL 的字符串缓存）；
- incr_with_expire（限流计数：INCR 自增 + 首次设过期）；
- __main__ 演示块，独立运行自测。
"""
import redis

from .config import REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD


def get_client():
    """创建并返回一个 Redis 连接客户端（注意要用完 close）。

    protocol=2：phpstudy 的 Redis 3.0 不支持新版 redis-py 默认发的 HELLO(RESP3) 握手，
    强制用老版 RESP2 协议以兼容服务端。
    decode_responses=True：读写自动按 utf-8 解码，返回 str 而非 bytes，使用更直观。
    """
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        password=REDIS_PASSWORD or None,  # 空串 -> None，表示无密码
        protocol=2,                      # 兼容老版本 Redis
        decode_responses=True,
    )


def cache_set(key, value, ttl=None):
    """写缓存。

    Args:
        key: 缓存键。
        value: 缓存值（字符串）。
        ttl: 过期秒数；None 表示永不过期。
    """
    client = get_client()
    try:
        client.set(key, value, ex=ttl)  # ex=ttl：设置过期时间；ttl=None 则永不过期
    finally:
        client.close()  # 无论成功失败都关闭连接，避免连接泄漏


def cache_get(key, default=None):
    """读缓存。

    Returns:
        命中返回字符串；未命中或已过期返回 default。
    """
    client = get_client()
    try:
        value = client.get(key)
        return value if value is not None else default
    finally:
        client.close()


def incr_with_expire(key, ttl):
    """限流计数：INCR 自增，首次创建时设置过期时间。

    用途：固定窗口限流。如"某 NPC 每 60 秒最多 N 次请求"。
    INCR 返回自增后的值；返回 1 说明是当前窗口第一次（新 key 或被重置），
    此时 EXPIRE 设过期时间，到期自动清零重新计数。
    """
    client = get_client()
    try:
        cur = client.incr(key)
        if cur == 1:
            client.expire(key, ttl)
        return cur
    finally:
        client.close()


if __name__ == "__main__":
    print("== 2.5 Redis 缓存演示 ==")

    cache_set("ctx:prince", "王子当前在宴会大厅", ttl=30)
    print("GET ctx:prince =", cache_get("ctx:prince"))
    print("GET ctx:missing =", cache_get("ctx:missing", "（未命中，返回默认值）"))

    client = get_client()
    print("ctx:prince 剩余 TTL（秒）=", client.ttl("ctx:prince"))
    client.close()

    print("\n限流计数（同一 key 5 秒内 incr 3 次）：")
    for _ in range(3):
        print("  incr =", incr_with_expire("rate:prince_chat", ttl=5))
