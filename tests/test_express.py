"""ExpressScanner against tests/fixtures/express_app (and the JS lexer it relies on)."""

from __future__ import annotations

import unittest
from pathlib import Path

from croqui.adapters import detect_stacks
from croqui.adapters._js import OrmIndex, lex, match_close
from croqui.adapters.express_js import ExpressScanner
from croqui.adapters.nestjs_ts import NestScanner
from croqui.model import Graph
from croqui.tables import build_registry

FIXTURES = Path(__file__).parent / "fixtures"
APP = FIXTURES / "express_app"


def scan(root: Path) -> Graph:
    registry = build_registry(root, code_suffixes=(".js", ".ts"))
    ExpressScanner.register_tables(root, registry)
    graph = Graph({"name": root.name})
    ExpressScanner(root, graph, registry).scan()
    return graph


class ExpressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = scan(APP)
        cls.edges = {(e["from"], e["to"], e["kind"]): e for e in cls.graph.edges}

    def test_detect(self) -> None:
        self.assertTrue(ExpressScanner.detect(APP))
        self.assertFalse(NestScanner.detect(APP))
        self.assertIn("node/express", detect_stacks(APP))
        self.assertNotIn("node/nestjs", detect_stacks(APP))

    def test_nest_app_is_not_express(self) -> None:
        nest = FIXTURES / "nest_app"
        self.assertFalse(ExpressScanner.detect(nest))
        self.assertTrue(NestScanner.detect(nest))
        self.assertNotIn("node/express", detect_stacks(nest))

    def test_routes_with_mount_prefixes(self) -> None:
        routes = {n for n in self.graph.nodes if n.startswith("route:")}
        self.assertEqual(
            routes,
            {
                "route:GET:/health",
                "route:GET:/users",
                "route:GET:/users/{id}",
                "route:POST:/users",
                "route:PUT:/users/{id}/profile",
                "route:DELETE:/users/{id}/profile",
                "route:GET:/users/{id}/posts",
                "route:POST:/users/{id}/posts",
                "route:GET:/api/reports/summary",
            },
        )

    def test_comments_and_strings_are_not_routes(self) -> None:
        for bad in ("/old-route", "/block-commented", "/in-string"):
            self.assertFalse([n for n in self.graph.nodes if bad in n])

    def test_route_meta(self) -> None:
        node = self.graph.nodes["route:GET:/users"]
        self.assertEqual(node["meta"]["handler"], "userController.list")
        self.assertEqual(node["meta"]["summary"], "List all users")
        self.assertEqual(node["meta"]["source"], "src/routes/users.js:9")
        self.assertEqual(node["meta"]["method"], "GET")
        self.assertEqual(node["group"], "users")
        self.assertTrue(self.graph.nodes["route:GET:/health"]["meta"]["standalone"])
        self.assertNotIn("standalone", node["meta"])

    def test_route_to_service_to_repo_to_table(self) -> None:
        for rid in ("route:GET:/users", "route:POST:/users", "route:DELETE:/users/{id}/profile"):
            self.assertIn((rid, "service:UserService", "calls"), self.edges)
        self.assertIn(("service:UserService", "repo:UserRepository", "calls"), self.edges)
        edge = self.edges[("repo:UserRepository", "table:users", "write")]
        self.assertEqual(edge["ops"], ["DELETE", "INSERT", "SELECT", "UPDATE"])  # @@map("users"), prisma.user.*

    def test_prisma_columns_use_map(self) -> None:
        cols = {c["name"]: c for c in self.graph.nodes["table:users"]["meta"]["columns"]}
        self.assertIn("full_name", cols)  # @map("full_name")
        self.assertTrue(cols["id"]["pk"])
        self.assertNotIn("posts", cols)  # relation field

    def test_knex_queries(self) -> None:
        sel = self.edges[("route:GET:/users/{id}/posts", "table:blog_posts", "read")]
        self.assertEqual(sel["ops"], ["SELECT"])  # Post model -> @@map("blog_posts")
        ins = self.edges[("route:POST:/users/{id}/posts", "table:audit_log", "write")]
        self.assertEqual(ins["ops"], ["INSERT"])  # createTable in a knex migration

    def test_raw_sql_and_external(self) -> None:
        rid = "route:GET:/api/reports/summary"
        self.assertEqual(self.edges[(rid, "table:leads", "read")]["ops"], ["SELECT"])  # DDL table
        self.assertIn((rid, "table:users", "read"), self.edges)
        self.assertIn((rid, "external:HTTP / Webhook", "send"), self.edges)

    def test_unknown_tables_are_dropped(self) -> None:
        tables = {n for n in self.graph.nodes if n.startswith("table:")}
        self.assertEqual(tables, {"table:users", "table:blog_posts", "table:audit_log", "table:leads"})


class LexerTests(unittest.TestCase):
    def test_comments_strings_regex_templates(self) -> None:
        src = (
            "const re = /[\"'`]\\/\\*/g; const t = `a ${`b ${c}`} // x`; // trailing\n"
            'router.get("/a", h); /* router.get("/b", h) */\n'
            "class A<T extends { a: number }> { m(): Promise<Array<{x:1}>> { return 1 } }\n"
        )
        lx = lex(src)
        self.assertEqual(len(lx.skel), len(src))
        self.assertNotIn("/b", lx.skel)
        self.assertNotIn("trailing", lx.skel)
        self.assertIn("router.get(", lx.skel)
        self.assertIn("/b", lx.text)
        open_brace = lx.skel.index("{ m()")
        self.assertEqual(lx.skel[match_close(lx.skel, open_brace)], "}")
        self.assertEqual(match_close(lx.skel, lx.skel.index("> {", lx.skel.index("class A")) + 2), len(lx.skel.rstrip()) - 1)

    def test_prisma_and_typeorm_registration(self) -> None:
        idx = OrmIndex.build(FIXTURES / "nest_app")
        self.assertEqual(idx.prisma, {"lead": "leads"})
        self.assertEqual(idx.entities["User"], "app_users")


class ExpressTypeScriptTests(unittest.TestCase):
    """ES imports, nested index routers, wrapped handlers, class instances, Sequelize / TypeORM."""

    FILES = {
        "package.json": '{"dependencies": {"express": "4"}}',
        "src/server.ts": """
import express from 'express';
import routes from './routes';
const app = express();
app.use('/v1', routes);
app.get(`/ping`, (_req: Request, res: Response): void => { res.send('pong'); });
""",
        "src/routes/index.ts": """
import { Router } from 'express';
import leadsRouter from './leads';
const router = Router();
router.use('/leads', leadsRouter);
export default router;
""",
        "src/routes/leads.ts": r"""
import { Router } from 'express';
import wrap from 'express-async-handler';
import { LeadController } from '../controllers/LeadController';
const router = Router();
const ctrl = new LeadController();
router.get('/', wrap(ctrl.list));
router.get('/:id(\d+)', ctrl.get.bind(ctrl));
router.delete('/:id', wrap(async (req, res) => { await Product.destroy({ where: {} }); }));
export default router;
""",
        "src/controllers/LeadController.ts": """
import { getRepository } from 'typeorm';
export class LeadController {
  list = async (req, res) => { res.json(await Product.findAll()); };
  async get(req, res) { const repo = getRepository(Invoice); res.json(await repo.findOne(1)); }
}
""",
        "src/models/Invoice.ts": """
@Entity('invoices')
export class Invoice { @PrimaryColumn() id: number; @Column() total: number; }
@Entity()
export class InvoiceLine { @PrimaryColumn() id: number; }
""",
        "src/models/Product.ts": """
export class Product extends Model {}
Product.init({ id: { type: DataTypes.INTEGER, primaryKey: true } }, { sequelize, modelName: 'Product' });
""",
    }

    def test_scan(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, body in self.FILES.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
            graph = scan(root)
            edges = {(e["from"], e["to"], e["kind"]): e["ops"] for e in graph.edges}
            self.assertEqual(
                {n for n in graph.nodes if n.startswith("route:")},
                {"route:GET:/ping", "route:GET:/v1/leads", "route:GET:/v1/leads/{id}", "route:DELETE:/v1/leads/{id}"},
            )
            # Sequelize default table: model name + "s"
            self.assertEqual(edges[("route:GET:/v1/leads", "table:products", "read")], ["SELECT"])
            self.assertEqual(edges[("route:GET:/v1/leads/{id}", "table:invoices", "read")], ["SELECT"])  # getRepository(Invoice)
            self.assertEqual(edges[("route:DELETE:/v1/leads/{id}", "table:products", "write")], ["DELETE"])
            registry = build_registry(root, code_suffixes=(".js", ".ts"))
            ExpressScanner.register_tables(root, registry)
            self.assertIsNotNone(registry.resolve("invoice_line"))  # bare @Entity() -> snake_case class name


if __name__ == "__main__":
    unittest.main()
