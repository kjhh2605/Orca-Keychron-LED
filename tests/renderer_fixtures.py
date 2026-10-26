"""Synthetic Orca renderer bundles in the two observed module layouts.

1.4.223: the Cmd+number handler imports the order function from another chunk.
1.4.241: the order function is declared and exported by the handler's chunk.
Names are hashed/minified like the real builds; only the structure matters.
"""
from orca_led.orca_compat import CompatError


INDEX_HTML = """<!doctype html><html><head>
<script type="module" crossorigin src="./assets/{entry}"></script>
<link rel="modulepreload" crossorigin href="./assets/{app}">
</head><body><div id="root"></div></body></html>
"""

# Not statically imported by the entry: never evaluated at startup, so any
# Cmd+number handler or store in it must be ignored.
LAZY_WEB = ("import{n as U}from\"./store-B_rT6dWz.js\";const Q=()=>{};"
            "const api={ui:{onJumpToWorktreeIndex:()=>Q}};"
            "function w(){window.api.ui.onJumpToWorktreeIndex(e=>{let t=U.getState();let n=z()[e]})}"
            "function z(){return[]}export{w as t,z as u};")

ORDER_241 = ("function e$(){if(ZQ)return ZQ;let e=U.getState();return e.rows.filter(e=>!e.isArchived)"
             ".map(e=>({id:e.id,...e.hostId?{executionHostId:e.hostId}:{}}))}")

LAYOUT_241 = {
    "index.html": INDEX_HTML.format(entry="index-BF0H8u7f.js", app="App-Dm3rSCHd.js"),
    "assets/index-BF0H8u7f.js": "import{t as re,tn as ie}from\"./App-Dm3rSCHd.js\";globalThis.__entry=[re,ie];",
    "assets/App-Dm3rSCHd.js": (
        "const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=[\"./web-CsCItEI0.js\",\"./store-B_rT6dWz.js\"])))"
        "=>i.map(i=>d[i]);"
        "import{n as U}from\"./store-B_rT6dWz.js\";var ZQ=null;"
        + ORDER_241 +
        "function vEe(e){}function xy(e,t){}"
        "function Ae(){let e=[];e.push(window.api.ui.onJumpToWorktreeIndex(e=>{let t=U.getState();"
        "if(t.activeModal===`worktree-palette`){vEe(e);return}if(t.activeView!==`terminal`)return;"
        "let n=e$()[e];n&&xy(n.id,{navigationIntent:`user-open`,showWorkspaceList:!0,"
        "...n.executionHostId?{executionHostId:n.executionHostId}:{}})}));"
        "e.push(window.api.ui.onJumpToTabIndex(e=>{U.getState().activeModal}));return e}"
        "function tn(){return`${Ae.name}`}"
        "export{e$ as br,Ae as t,tn};\n//# sourceMappingURL=App-Dm3rSCHd.js.map\n"),
    "assets/store-B_rT6dWz.js": "const n={getState:()=>globalThis.__state};export{n};",
    "assets/web-CsCItEI0.js": LAZY_WEB,
}
EXPECTED_241 = {"entry": "assets/index-BF0H8u7f.js", "order_module": "assets/App-Dm3rSCHd.js",
                "order_export": "br", "order_source": ORDER_241,
                "store_module": "assets/store-B_rT6dWz.js", "store_export": "n"}

ORDER_223 = ("function Rs(){if(Ns)return Ns;let e=F.getState();return e.rows.filter(e=>!e.isArchived)"
             ".map(e=>({id:e.id,...e.hostId?{executionHostId:e.hostId}:{}}))}")

LAYOUT_223 = {
    "index.html": INDEX_HTML.format(entry="index-lGZcUCDD.js", app="App-BGnLbgfw.js"),
    "assets/index-lGZcUCDD.js": "import{t as re}from\"./App-BGnLbgfw.js\";globalThis.__entry=re;",
    "assets/App-BGnLbgfw.js": (
        "import{t as H}from\"./store-J2Yw4OnJ.js\";"
        "import{DC as s,s as Ul,t as Wl}from\"./worktree-activation-D0sk4KxD.js\";"
        "function hg(e){}function Nl(e,t){}"
        "function mount(){let e=[];e.push(window.api.ui.onJumpToWorktreeIndex(e=>{let t=H.getState();"
        "if(t.activeModal===`worktree-palette`){hg(e);return}if(t.activeView!==`terminal`)return;"
        "let n=Ul()[e];n&&Nl(n.id,{navigationIntent:`user-open`})}));return e}"
        "export{mount as t};"),
    "assets/worktree-activation-D0sk4KxD.js": (
        "import{t as F}from\"./store-J2Yw4OnJ.js\";var Ns=null;const DC=1;function Wl(){}"
        + ORDER_223 +
        "export{DC,Rs as s,Wl as t};"),
    "assets/store-J2Yw4OnJ.js": "var h={getState:()=>globalThis.__state};export{h as t};",
}
EXPECTED_223 = {"entry": "assets/index-lGZcUCDD.js",
                "order_module": "assets/worktree-activation-D0sk4KxD.js",
                "order_export": "s", "order_source": ORDER_223,
                "store_module": "assets/store-J2Yw4OnJ.js", "store_export": "t"}


def reader(files: dict):
    def read(path: str) -> bytes:
        if path not in files:
            raise CompatError("orca_layout_changed", f"No {path}")
        return files[path].encode("utf-8")
    return read
