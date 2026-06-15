"""统一凭证刷新 CLI：python auth.py [doubao|deepseek|all]"""

import argparse
import asyncio
import sys

from providers import get_provider, list_providers


async def _refresh_one(provider_id: str) -> None:
    provider = get_provider(provider_id)
    await provider.startup(refresh_credentials=True)
    print(f"[auth] {provider.display_name} 凭证已刷新")


async def _main(provider: str) -> None:
    if provider == "all":
        for p in list_providers():
            try:
                await _refresh_one(p.id)
            except Exception as exc:
                print(f"[auth] {p.display_name} 失败: {exc}", file=sys.stderr)
        return
    await _refresh_one(provider)


def main() -> None:
    ids = [p.id for p in list_providers()]
    parser = argparse.ArgumentParser(description="刷新 AI 平台浏览器登录凭证")
    parser.add_argument(
        "provider",
        nargs="?",
        default="all",
        choices=[*ids, "all"],
        help=f"平台: {', '.join(ids)} 或 all（默认）",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.provider))


if __name__ == "__main__":
    main()
