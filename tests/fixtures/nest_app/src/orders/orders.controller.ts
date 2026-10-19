import { Controller, Get, Param, Post } from '@nestjs/common';
import { AuditService } from '../audit/audit.service';
import { OrdersService } from './orders.service';

@Controller({ path: 'orders' })
export class OrdersController {
  constructor(
    private readonly ordersService: OrdersService,
    private readonly auditService: AuditService,
  ) {}

  @Get(':id/items')
  items(@Param('id') id: string) {
    return this.ordersService.items(id);
  }

  @Post()
  place() {
    return this.ordersService.place();
  }

  @Get()
  health() {
    return { ok: true };
  }
}
