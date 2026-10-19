const router = require("express").Router();
const knex = require("../db/knex");

router.get("/", async (req, res) => {
  const rows = await knex("blog_posts").select("*").where({ authorId: req.params.id });
  res.json(rows);
});

router.post("/", async (req, res) => {
  await knex("audit_log").insert({ action: "post.create" });
  res.status(201).end();
});

module.exports = router;
