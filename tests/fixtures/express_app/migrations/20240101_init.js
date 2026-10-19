exports.up = function (knex) {
  return knex.schema.createTable("audit_log", (t) => {
    t.increments("id");
    t.string("action").notNullable();
    t.integer("user_id");
  });
};
