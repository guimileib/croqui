import { Controller, Get, Param, Post } from '@nestjs/common';
import { AuditService } from '../audit/audit.service';
import { LeadsService } from './leads.service';

@Controller({ path: 'leads' })
export class LeadsController {
  constructor(
    private readonly leadsService: LeadsService,
    private readonly auditService: AuditService,
  ) {}

  @Get(':id/items')
  items(@Param('id') id: string) {
    return this.leadsService.items(id);
  }

  @Post()
  place() {
    return this.leadsService.place();
  }

  @Get()
  health() {
    return { ok: true };
  }
}
