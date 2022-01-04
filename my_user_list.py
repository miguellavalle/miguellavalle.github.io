import collections


class MiLista(collections.UserList):
        
    def __iter__(self):
        print("list length %s" % len(self))
        next_item = 0
        try:
            while True:
                value = self[next_item]
                yield value
                next_item += 1
        except IndexError:
            return
        
    def reset_next(self, value):
        if value < 0:
            raise ValueError("List index cannot be less that 0")
        self._userlist_next = value
        