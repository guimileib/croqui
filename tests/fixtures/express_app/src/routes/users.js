const express = require("express");
const userController = require("../controllers/userController");
const postsRouter = require("./posts");
const { requireAuth } = require("../middleware/auth");

const router = express.Router();

/** List all users */
router.get("/", userController.list);
router.get("/:id", requireAuth, userController.get);
router.post("/", requireAuth, userController.create);
router
  .route("/:id/profile")
  .put(userController.update)
  .delete(userController.remove);

router.use("/:id/posts", postsRouter);

module.exports = router;
