const prisma = require("../db/prisma");

class UserRepository {
  findAll() {
    return prisma.user.findMany();
  }

  findById(id) {
    return prisma.user.findUnique({ where: { id } });
  }

  create(data) {
    return prisma.user.create({ data });
  }

  update(id, data) {
    return prisma.user.update({ where: { id }, data });
  }

  remove(id) {
    return prisma.user.delete({ where: { id } });
  }
}

module.exports = UserRepository;
