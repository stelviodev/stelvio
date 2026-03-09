import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'DeleteItem',
        key: util.dynamodb.toMapValues({ id: ctx.args.id }),
    };
}

export function response(ctx) {
    return ctx.prev.result;
}
