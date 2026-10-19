const UserRepository = require("../repositories/UserRepository");

class UserService {
  constructor() {
    this.repo = new UserRepository();
  }

  list() {
    return this.repo.findAll();
  }

  get(id) {
    return this.repo.findById(id);
  }

  create(data) {
    return this.repo.create(data);
  }

  rename(id, name) {
    return this.repo.update(id, { name });
  }

  remove(id) {
    return this.repo.remove(id);
  }
}

module.exports = new UserService();
