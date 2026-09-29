src = open(r".venv\Lib\site-packages\lightrag\kg\postgres_impl.py", encoding="utf-8").read()
i = src.find("class PGGraphStorage")
seg = src[i : i + 60000]
k = seg.find("async def initialize")
print(seg[k : k + 2600])
