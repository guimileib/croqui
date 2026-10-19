import { Injectable } from '@nestjs/common';
import { PrismaService } from '../prisma/prisma.service';

@Injectable()
export class LeadsRepository {
  constructor(private readonly prisma: PrismaService) {}

  findById(id: number) {
    return this.prisma.lead.findUnique({ where: { id } });
  }

  create(data: { total: number; userId: number }) {
    return this.prisma.lead.create({ data });
  }
}
