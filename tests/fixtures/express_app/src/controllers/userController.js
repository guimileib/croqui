const userService = require("../services/UserService");

exports.list = async (req, res) => {
  res.json(await userService.list());
};

async function get(req, res) {
  res.json(await userService.get(Number(req.params.id)));
}

exports.get = get;

exports.create = async (req, res) => {
  const user = await userService.create(req.body);
  res.status(201).json(user);
};

exports.update = async (req, res) => {
  res.json(await userService.rename(req.params.id, req.body.name));
};

exports.remove = async (req, res) => {
  await userService.remove(req.params.id);
  res.status(204).end();
};
