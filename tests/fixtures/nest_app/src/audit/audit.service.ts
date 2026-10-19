import { Injectable } from '@nestjs/common';

@Injectable()
export class AuditService {
  record(action: string) {
    return action;
  }
}
