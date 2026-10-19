import { Column, Entity, PrimaryGeneratedColumn } from 'typeorm';

@Entity({ name: 'app_users', schema: 'core' })
export class User {
  @PrimaryGeneratedColumn()
  id: number;

  @Column({ name: 'email_address', type: 'varchar' })
  email: string;

  @Column()
  name: string;
}
