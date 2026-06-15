"""DeepSeek 网页 PoW 求解（wasmtime + 官方 WASM）。"""

import base64
import json
import struct
import urllib.request
from dataclasses import dataclass

from config import DEEPSEEK

_solver: "PowSolver | None" = None


@dataclass(frozen=True)
class PowChallenge:
    algorithm: str
    challenge: str
    salt: str
    signature: str
    difficulty: int
    expire_at: int
    target_path: str


class PowSolver:
    def __init__(self) -> None:
        self._engine = None
        self._module = None
        self._wasm_bytes: bytes | None = None

    def _load_bytes(self) -> bytes:
        if self._wasm_bytes is None:
            with urllib.request.urlopen(DEEPSEEK.wasm_url, timeout=30) as resp:
                self._wasm_bytes = resp.read()
        return self._wasm_bytes

    def warmup(self) -> None:
        if self._module is not None:
            return
        try:
            from wasmtime import Engine, Module
        except ImportError as exc:
            raise RuntimeError("缺少 wasmtime：pip install wasmtime") from exc
        self._engine = Engine()
        self._module = Module(self._engine, self._load_bytes())

    @staticmethod
    def _write(ptr: int, data: bytes, memory, store) -> None:
        raw = memory.data_ptr(store)
        for i, b in enumerate(data):
            raw[ptr + i] = b

    @staticmethod
    def _read(ptr: int, size: int, memory, store) -> bytes:
        raw = memory.data_ptr(store)
        return bytes(raw[ptr + i] for i in range(size))

    def _pass_string(self, value: str, malloc, realloc, memory, store) -> tuple[int, int]:
        encoded = value.encode("utf-8")
        if all(b < 128 for b in encoded):
            ptr = malloc(store, len(encoded), 1)
            self._write(ptr, encoded, memory, store)
            return ptr, len(encoded)

        ptr = malloc(store, len(value), 1)
        raw = memory.data_ptr(store)
        offset = 0
        while offset < len(value):
            ch = ord(value[offset])
            if ch > 127:
                break
            raw[ptr + offset] = ch
            offset += 1

        if offset != len(value):
            remainder = value[offset:].encode("utf-8")
            ptr = realloc(store, ptr, len(value), offset + len(remainder), 1)
            self._write(ptr + offset, remainder, memory, store)
            return ptr, offset + len(remainder)
        return ptr, offset

    def solve(self, challenge: PowChallenge) -> int:
        if self._module is None:
            self.warmup()

        from wasmtime import Instance, Store

        store = Store(self._engine)
        instance = Instance(store, self._module, [])
        exports = instance.exports(store)

        memory = exports["memory"]
        malloc = exports["__wbindgen_export_0"]
        realloc = exports["__wbindgen_export_1"]
        stack_ptr_fn = exports["__wbindgen_add_to_stack_pointer"]
        wasm_solve = exports["wasm_solve"]

        challenge_ptr, challenge_len = self._pass_string(
            challenge.challenge, malloc, realloc, memory, store
        )
        prefix_ptr, prefix_len = self._pass_string(
            f"{challenge.salt}_{challenge.expire_at}_",
            malloc,
            realloc,
            memory,
            store,
        )

        ret_ptr = stack_ptr_fn(store, -16)
        try:
            wasm_solve(
                store,
                ret_ptr,
                challenge_ptr,
                challenge_len,
                prefix_ptr,
                prefix_len,
                float(challenge.difficulty),
            )
            raw = self._read(ret_ptr, 16, memory, store)
            ok = struct.unpack_from("<i", raw, 0)[0]
            answer = struct.unpack_from("<d", raw, 8)[0]
        finally:
            stack_ptr_fn(store, 16)

        if ok != 1:
            raise RuntimeError("DeepSeek PoW 求解失败")
        return int(answer)

    def build_header(self, challenge: PowChallenge) -> str:
        answer = self.solve(challenge)
        payload = {
            "algorithm": challenge.algorithm,
            "challenge": challenge.challenge,
            "salt": challenge.salt,
            "answer": answer,
            "signature": challenge.signature,
            "target_path": challenge.target_path,
        }
        return base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")


def get_solver() -> PowSolver:
    global _solver
    if _solver is None:
        _solver = PowSolver()
        _solver.warmup()
    return _solver
