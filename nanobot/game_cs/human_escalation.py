from __future__ import annotations

import httpx


async def forward_to_admin(
    query_id: int,
    user_id: str,
    question: str,
    admin_url: str,
    token: str,
    timeout_s: float = 10.0,
) -> bool:
    payload = {
        "text": (
            "【客服待处理咨询】  \n"
            f"Query ID: {query_id}  \n"
            f"用户ID：{user_id}  \n"
            f"问题：{question}  \n  \n"
            "可以使用说：  \n"
            f"回复{user_id} , 你的问题已经解决  \n"
            f"{user_id} 关闭AI自动回复  \n" 
        )
    }
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(admin_url, json=payload, headers=headers)
            return resp.status_code < 400
    except Exception:
        return False
