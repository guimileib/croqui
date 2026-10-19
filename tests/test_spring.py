"""Spring Boot scanner: routes, DI wiring, repositories, entities (Java and Kotlin)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from croqui.adapters.spring_java import SpringScanner, _lex, _snake
from croqui.model import Graph
from croqui.tables import build_registry

FIXTURES = Path(__file__).parent / "fixtures"
SPRING_POM = "<artifactId>spring-boot-starter</artifactId>"


def scan(root: Path) -> tuple[Graph, SpringScanner]:
    registry = build_registry(root, code_suffixes=(".py", ".java", ".kt"))
    SpringScanner.register_tables(root, registry)
    graph = Graph({"name": root.name})
    scanner = SpringScanner(root, graph, registry)
    scanner.scan()
    return graph, scanner


def edges(graph: Graph) -> dict[tuple[str, str], dict]:
    return {(e["from"], e["to"]): e for e in graph.edges}


def write_project(files: dict[str, str]) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    for name, text in files.items():
        path = Path(tmp.name) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp


class JavaFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = FIXTURES / "spring_app"
        cls.graph, cls.scanner = scan(cls.root)
        cls.nodes = cls.graph.nodes
        cls.edges = edges(cls.graph)

    def test_detect(self) -> None:
        self.assertTrue(SpringScanner.detect(self.root))
        self.assertTrue(SpringScanner.detect(FIXTURES / "spring_kotlin"))

    def test_routes_carry_context_path_and_constants(self) -> None:
        routes = {n for n in self.nodes if n.startswith("route:")}
        self.assertEqual(
            routes,
            {
                "route:GET:/api/v1/users/{id}",  # ApiPaths.USERS = BASE + "/users", {id:[0-9]+} cleaned
                "route:POST:/api/v1/users",
                "route:PUT:/api/v1/users/{id}",  # path = {"/{id}", "/legacy/{id}"} -> first
                "route:DELETE:/api/v1/users/{id}",  # @RequestMapping(method = DELETE, value = ...)
                "route:GET:/api/v1/users/ping",
                "route:GET:/api/orders",
                "route:POST:/api/orders/{id}/cancel",
                "route:GET:/api/orders/health",
            },
        )

    def test_commented_and_string_mappings_are_ignored(self) -> None:
        for name in self.nodes:
            self.assertNotIn("commented-out", name)
            self.assertNotIn("block-commented", name)
            self.assertNotIn("in-string", name)
            self.assertNotIn("should-not-appear", name)  # src/test is skipped

    def test_route_meta(self) -> None:
        route = self.nodes["route:GET:/api/v1/users/{id}"]["meta"]
        self.assertEqual(route["method"], "GET")
        self.assertEqual(route["handler"], "get")
        self.assertEqual(route["summary"], "Fetch one user.")
        self.assertIn("404", route["description"])
        self.assertTrue(route["source"].endswith("UserController.java:23"))
        self.assertEqual(route["params"], [{"name": "id", "in": "path", "required": True, "type": "Long"}])
        self.assertEqual(self.nodes["route:POST:/api/v1/users"]["meta"]["request_body"], "CreateUser")
        self.assertEqual(self.nodes["route:PUT:/api/v1/users/{id}"]["meta"]["response_model"], "UserDto")

    def test_route_to_service_edges(self) -> None:
        self.assertIn(("route:GET:/api/v1/users/{id}", "service:UserService"), self.edges)
        self.assertIn(("route:POST:/api/orders/{id}/cancel", "service:OrderService"), self.edges)  # Lombok ctor
        self.assertIn(("route:GET:/api/orders", "service:OrderService"), self.edges)

    def test_handler_without_collaborators_is_standalone(self) -> None:
        for rid in ("route:GET:/api/orders/health", "route:GET:/api/v1/users/ping"):
            self.assertTrue(self.nodes[rid]["meta"].get("standalone"))
            self.assertFalse([k for k in self.edges if k[0] == rid])

    def test_interface_collapses_to_impl_body(self) -> None:
        self.assertNotIn("service:UserServiceImpl", self.nodes)
        self.assertEqual(self.nodes["service:UserService"]["meta"]["impl"], ["UserServiceImpl"])
        # the repository is injected into the impl (@Autowired field), not the interface
        self.assertIn(("service:UserService", "repo:UserRepository"), self.edges)

    def test_spring_data_repository_ops_and_default_naming(self) -> None:
        edge = self.edges[("repo:UserRepository", "table:core.tb_users")]
        self.assertEqual(edge["kind"], "write")
        # findById/findByEmailContaining/native SELECT -> SELECT, save -> INSERT+UPDATE, deleteById -> DELETE
        self.assertEqual(sorted(edge["ops"]), ["DELETE", "INSERT", "SELECT", "UPDATE"])
        self.assertEqual(self.nodes["repo:UserRepository"]["meta"]["entity"], "User")

    def test_jdbc_repository_text_block_and_concatenated_sql(self) -> None:
        self.assertIn(("service:OrderService", "repo:OrderJdbcRepository"), self.edges)
        edge = self.edges[("repo:OrderJdbcRepository", "table:orders")]
        self.assertEqual(sorted(edge["ops"]), ["SELECT", "UPDATE"])
        self.assertNotIn("unresolved", self.nodes["repo:OrderJdbcRepository"]["meta"])

    def test_external_http_client(self) -> None:
        edge = self.edges[("service:OrderService", "external:HTTP / Webhook")]
        self.assertEqual(edge["kind"], "send")

    def test_entity_columns(self) -> None:
        cols = {c["name"]: c for c in self.nodes["table:core.tb_users"]["meta"]["columns"]}
        self.assertEqual(list(cols), ["id", "email_address", "display_name"])  # @Transient/@OneToMany skipped
        self.assertTrue(cols["id"].get("pk"))
        self.assertTrue(cols["email_address"].get("required"))
        self.assertEqual(self.nodes["table:core.tb_users"]["meta"]["schema"], "core")

    def test_default_snake_case_entity_registered(self) -> None:
        registry = build_registry(self.root, code_suffixes=(".java",))
        SpringScanner.register_tables(self.root, registry)
        rec = registry.resolve("order_item")
        self.assertIsNotNone(rec)
        cols = {c["name"]: c for c in rec["columns"]}
        self.assertEqual(list(cols), ["id", "owner_id", "unit_price", "product_name"])
        self.assertEqual(cols["owner_id"]["references"], "tb_users")
        self.assertEqual(registry.qualified("tb_users"), "core.tb_users")
        # DDL from Flyway stays authoritative for its own table
        self.assertEqual(registry.resolve("orders")["columns"][0]["type"], "BIGSERIAL")


class KotlinFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph, _ = scan(FIXTURES / "spring_kotlin")
        cls.nodes = cls.graph.nodes
        cls.edges = edges(cls.graph)

    def test_routes(self) -> None:
        routes = {n for n in self.nodes if n.startswith("route:")}
        self.assertEqual(
            routes,
            {
                "route:GET:/products/{id}",  # const val inside an object
                "route:GET:/products/search/{name}",
                "route:POST:/products",
                "route:GET:/products/status",
            },
        )

    def test_chain(self) -> None:
        self.assertIn(("route:GET:/products/{id}", "service:ProductService"), self.edges)
        self.assertIn(("route:GET:/products/search/{name}", "service:ProductService"), self.edges)  # template string
        self.assertIn(("service:ProductService", "repo:ProductRepository"), self.edges)
        self.assertTrue(self.nodes["route:GET:/products/status"]["meta"].get("standalone"))
        edge = self.edges[("repo:ProductRepository", "table:products")]
        self.assertEqual(sorted(edge["ops"]), ["DELETE", "INSERT", "SELECT", "UPDATE"])  # JPQL DELETE, save, find

    def test_kotlin_columns(self) -> None:
        cols = {c["name"]: c for c in self.nodes["table:products"]["meta"]["columns"]}
        self.assertEqual(list(cols), ["id", "product_name", "unit_price", "category_id"])
        self.assertTrue(cols["id"].get("pk"))
        self.assertTrue(cols["product_name"].get("required"))
        self.assertEqual(cols["category_id"]["references"], "category")

    def test_request_params(self) -> None:
        params = self.nodes["route:GET:/products/search/{name}"]["meta"]["params"]
        self.assertEqual(
            [(p["name"], p["in"], p["required"]) for p in params],
            [("name", "path", True), ("page", "query", False)],
        )


class NotSpringTest(unittest.TestCase):
    def test_other_fixtures_not_detected(self) -> None:
        for name in ("fastapi_only", "flask_app", "django_app"):
            path = FIXTURES / name
            if path.is_dir():
                self.assertFalse(SpringScanner.detect(path), name)

    def test_plain_maven_project_not_detected(self) -> None:
        files = {"pom.xml": "<project><artifactId>lib</artifactId></project>", "src/main/java/A.java": "class A {}"}
        with write_project(files) as tmp:
            self.assertFalse(SpringScanner.detect(Path(tmp)))

    def test_spring_boot_application_annotation_detects(self) -> None:
        with write_project({"src/App.kt": "@SpringBootApplication\nclass App"}) as tmp:
            self.assertTrue(SpringScanner.detect(Path(tmp)))


class LexerTest(unittest.TestCase):
    def test_comments_and_strings(self) -> None:
        raw = 'a // x {\nb /* { */ c "}" \'{\' """\n{\n""" d'
        clean, skel, _ = _lex(raw, kotlin=False)
        self.assertEqual(len(clean), len(raw))
        self.assertNotIn("x {", clean)
        self.assertNotIn("{", skel)
        self.assertNotIn("}", skel)
        self.assertIn('"}"', clean)

    def test_kotlin_templates_and_nested_comments(self) -> None:
        raw = 'val s = "a ${"}"} b" /* x /* y */ { */ val t = 1'
        _, skel, _ = _lex(raw, kotlin=True)
        self.assertNotIn("}", skel)
        self.assertIn("val t = 1", skel)

    def test_snake(self) -> None:
        self.assertEqual(_snake("OrderItem"), "order_item")
        self.assertEqual(_snake("userAccountId"), "user_account_id")
        self.assertEqual(_snake("email"), "email")


class EdgeCaseTest(unittest.TestCase):
    def test_unresolved_constant_is_flagged(self) -> None:
        files = {
            "pom.xml": SPRING_POM,
            "src/main/java/C.java": (
                "import x.Missing;\n@RestController\n"
                '@RequestMapping(Missing.BASE + "/a")\nclass C {\n'
                '  @GetMapping("/b") String b() { return ""; }\n}\n'
            ),
        }
        with write_project(files) as tmp:
            graph, _ = scan(Path(tmp))
        node = graph.nodes["route:GET:/{Missing.BASE}/a/b"]
        self.assertTrue(node["meta"]["unresolved"])
        self.assertIn("Missing.BASE", node["meta"]["hint"])

    def test_request_mapping_without_method_and_generics(self) -> None:
        files = {
            "pom.xml": SPRING_POM,
            "src/main/java/C.java": (
                "@Controller\nclass C {\n"
                "  private final java.util.Map<String, java.util.List<Integer>> cache = new java.util.HashMap<>();\n"
                '  @RequestMapping("/any") public Map<String, List<Integer>> any() { if (true) { return null; } return cache; }\n'
                '  @RequestMapping(value = "/two", method = {RequestMethod.GET, RequestMethod.POST}) void two() {}\n'
                '  @GetMapping("/after") void after() {}\n}\n'
            ),
        }
        with write_project(files) as tmp:
            graph, _ = scan(Path(tmp))
        routes = {n for n in graph.nodes if n.startswith("route:")}
        self.assertEqual(routes, {"route:ANY:/any", "route:GET:/two", "route:POST:/two", "route:GET:/after"})

    def test_repo_without_methods_defaults_to_select_and_jpql_join_is_not_a_table(self) -> None:
        files = {
            "pom.xml": SPRING_POM,
            "src/main/java/Account.java": "@Entity\npublic class Account { @Id Long id; String ownerName; }\n",
            "src/main/java/Item.java": '@Entity\n@Table(name = "items")\npublic class Item { @Id Long id; }\n',
            "src/main/java/AccountRepository.java": "public interface AccountRepository extends CrudRepository<Account, Long> {}\n",
            "src/main/java/ItemRepository.java": (
                "public interface ItemRepository extends JpaRepository<Item, Long> {\n"
                '  @Query("select i from Item i join i.account a where a.id = :id") List<Item> byAccount(Long id);\n'
                "  void deleteByAccountId(Long id);\n}\n"
            ),
        }
        with write_project(files) as tmp:
            graph, _ = scan(Path(tmp))
        e = edges(graph)
        self.assertEqual(e[("repo:AccountRepository", "table:account")]["ops"], ["SELECT"])
        self.assertEqual(e[("repo:AccountRepository", "table:account")]["kind"], "read")
        self.assertEqual(sorted(e[("repo:ItemRepository", "table:items")]["ops"]), ["DELETE", "SELECT"])
        self.assertNotIn(("repo:ItemRepository", "table:account"), e)

    def test_mybatis_mapper_xml_and_annotations(self) -> None:
        files = {
            "pom.xml": SPRING_POM,
            "db/V1__x.sql": "CREATE TABLE users (id int);\nCREATE TABLE audits (id int);",
            "src/main/java/UserMapper.java": (
                "import org.apache.ibatis.annotations.*;\n@Mapper\npublic interface UserMapper {\n"
                '  @Select("SELECT * FROM users WHERE id = #{id}") Object byId(long id);\n'
                "  void audit(Object a);\n}\n"
            ),
            "src/main/resources/mapper/UserMapper.xml": (
                '<mapper namespace="com.x.UserMapper"><insert id="audit">'
                "<![CDATA[INSERT INTO audits (id) VALUES (1)]]></insert></mapper>"
            ),
        }
        with write_project(files) as tmp:
            graph, _ = scan(Path(tmp))
        e = edges(graph)
        self.assertEqual(e[("repo:UserMapper", "table:users")]["ops"], ["SELECT"])
        self.assertEqual(e[("repo:UserMapper", "table:audits")]["ops"], ["INSERT"])

    def test_spring_data_jdbc_record_and_properties_context_path(self) -> None:
        files = {
            "build.gradle": "plugins { id 'org.springframework.boot' }",
            "src/main/resources/application.properties": "server.servlet.context-path=/svc\nspring.mvc.servlet.path=/rest\n",
            "src/main/java/Person.java": (
                "import org.springframework.data.relational.core.mapping.Table;\n"
                '@Table("people")\npublic record Person(@Id Long id, String fullName) {}\n'
            ),
            "src/main/java/PersonRepository.java": "interface PersonRepository extends CrudRepository<Person, Long> {}\n",
            "src/main/java/PersonController.java": (
                "@RestController\nclass PersonController {\n  @Autowired PersonRepository people;\n"
                '  @GetMapping("/people") Object all() { return people.findAll(); }\n}\n'
            ),
        }
        with write_project(files) as tmp:
            graph, _ = scan(Path(tmp))
        e = edges(graph)
        self.assertIn(("route:GET:/svc/rest/people", "repo:PersonRepository"), e)
        self.assertEqual(e[("repo:PersonRepository", "table:people")]["ops"], ["SELECT"])
        cols = [c["name"] for c in graph.nodes["table:people"]["meta"]["columns"]]
        self.assertEqual(cols, ["id", "full_name"])



    def test_kotlin_autowired_property_expression_body_and_allman_java(self) -> None:
        kt = (
            '@RestController\n@RequestMapping(value = ["/a"])\nclass AController {\n'
            "    @Autowired lateinit var svc: ThingService\n"
            '    @GetMapping("/y") fun y() = svc.go()\n'
            '    @GetMapping("/z")\n    fun z(): String = "z"\n}\n'
            "@Service class ThingService { fun go() = 1 }\n"
        )
        java = (
            "@RestController\npublic class BController\n{\n    @Autowired\n    private ThingService svc;\n\n"
            '    @GetMapping(value = "/b")\n    public String b()\n    {\n        return svc.go();\n    }\n}\n'
        )
        with write_project({"pom.xml": SPRING_POM, "src/A.kt": kt, "src/B.java": java}) as tmp:
            graph, _ = scan(Path(tmp))
        e = edges(graph)
        self.assertIn(("route:GET:/a/y", "service:ThingService"), e)
        self.assertIn(("route:GET:/b", "service:ThingService"), e)
        self.assertTrue(graph.nodes["route:GET:/a/z"]["meta"]["standalone"])

if __name__ == "__main__":
    unittest.main()
