import { Injectable } from '@nestjs/common';
import { LeadsRepository } from './leads.repository';

@Injectable()
export class LeadsService {
  constructor(private readonly leadsRepository: LeadsRepository) {}

  notes(id: string) {
    return this.leadsRepository.findById(Number(id));
  }

  place() {
    return this.leadsRepository.create({ total: 10, userId: 1 });
  }
}
