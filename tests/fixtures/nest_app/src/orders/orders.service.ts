import { Injectable } from '@nestjs/common';
import { OrdersRepository } from './orders.repository';

@Injectable()
export class OrdersService {
  constructor(private readonly ordersRepository: OrdersRepository) {}

  items(id: string) {
    return this.ordersRepository.findById(Number(id));
  }

  place() {
    return this.ordersRepository.create({ total: 10, userId: 1 });
  }
}
